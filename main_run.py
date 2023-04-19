import argparse
import torch
from diffusers import StableDiffusionPipeline
from diffusers import DDIMScheduler
import os
from prompt_to_prompt.ptp_classes import AttentionStore, AttentionReplace, AttentionRefine, EmptyControl
from prompt_to_prompt.ptp_utils import register_attention_control, text2image_ldm_stable, view_images
from prompt_to_prompt.ptp_classes import show_cross_attention
from prompt_to_prompt.ptp_classes import load_512

# NUM_DIFFUSION_STEPS = 2


from prompt_to_prompt.inversion_utils import load_real_image, inversion_forward_process, inversion_reverse_process
from prompt_to_prompt.utils import image_grid
import diffusers

from torch import autocast, inference_mode
from prompt_to_prompt.ddim_inversion import ddim_inversion

from prompt_to_prompt.utils import load_dataset, create_prompts_from_class, dataset_from_yaml


import openpyxl

import calendar
import time

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--device_num", type=int, default=2)
    parser.add_argument("--cfg_enc", type=float, default=3.5)
    parser.add_argument("--cfg_dec", type=float, default=[15])
    parser.add_argument("--num_diffusion_steps", type=int, default=100)
    parser.add_argument("--prompt_enc",  default="a cat sitting next to a mirror")
    parser.add_argument("--prompt_dec",  default="a tiger sitting next to a mirror")
    parser.add_argument("--dataset_yaml",  default="test.yaml")
    parser.add_argument("--img_name",  default="example_image/gnochi_mirror.jpeg")
    parser.add_argument("--eta", type=float, default=1)
    parser.add_argument("--mode",  default="p2pinv", help="modes: our_inv,p2pinv,p2pddim, ddim")
    parser.add_argument("--skip",  type=int, default=[36])
    parser.add_argument("--xa", type=float, default=0.6)
    parser.add_argument("--sa", type=float, default=0.2)
    

    args = parser.parse_args()
    full_data = dataset_from_yaml(args.dataset_yaml)

    # create scheduler
    # load diffusion model
    model_id = "CompVis/stable-diffusion-v1-4"
    # model_id = "stable_diff_local" # load local save of model (for internet problems)

    device = f"cuda:{args.device_num}"

    cfg_scale_enc = args.cfg_enc
    cfg_scale_dec_list = args.cfg_dec
    eta = args.eta #1
    skip_zs=args.skip
    xa_sa_string = f'_xa_{args.xa}_sa{args.sa}_' if args.mode=='p2pinv' else '_'


    current_GMT = time.gmtime()
    time_stamp = calendar.timegm(current_GMT)

    for i in range(len(full_data)):
        current_image_data = full_data[i]
        image_path = current_image_data['init_img']
        image_path = '.' + image_path 
        image_folder = image_path.split('/')[1] # after '.'
        prompt_enc = current_image_data.get('source_prompt', "") # default empty string
        prompt_dec_list = current_image_data['target_prompts']

        # load/reload model:
        ldm_stable = StableDiffusionPipeline.from_pretrained(model_id).to(device)

        if args.mode=="p2pddim" or args.mode=="ddim":
            scheduler = DDIMScheduler(beta_start=0.00085, beta_end=0.012, beta_schedule="scaled_linear", clip_sample=False, set_alpha_to_one=False)
            ldm_stable.scheduler = scheduler
        else:
            ldm_stable.scheduler = DDIMScheduler.from_config(model_id, subfolder = "scheduler")
            
        ldm_stable.scheduler.set_timesteps(args.num_diffusion_steps)

        # load image
        offsets=(0,0,0,0)
        x0 = load_512(image_path, *offsets, device)

        # vae encode image
        with autocast("cuda"), inference_mode():
            w0 = (ldm_stable.vae.encode(x0).latent_dist.mode() * 0.18215).float()

        # find Zs and wts - forward process
        if args.mode=="p2pddim" or args.mode=="ddim":
            wT = ddim_inversion(ldm_stable, w0, prompt_enc, cfg_scale_enc)
        else:
            wt, zs, wts = inversion_forward_process(ldm_stable, w0, etas=eta, prompt=prompt_enc, cfg_scale=cfg_scale_enc, prog_bar=True, num_inference_steps=args.num_diffusion_steps)

        # iterate over decoder prompts
        for k in range(len(prompt_dec_list)):
            prompt_dec = prompt_dec_list[k]
            save_path = os.path.join(f'./results_{args.num_diffusion_steps}/', args.mode+xa_sa_string+str(time_stamp), image_path.split(sep='.')[0], 'enc_' + prompt_enc.replace(" ", "_"), 'dec_' + prompt_dec.replace(" ", "_"))
            os.makedirs(save_path, exist_ok=True)

            # Check if number of words in encoder and decoder text are equal
            enc_dec_len_eq = (len(prompt_enc.split(" ")) == len(prompt_dec.split(" ")))

            for cfg_scale_dec in cfg_scale_dec_list:
                for skip in skip_zs:    
                    if args.mode=="our_inv":
                        # reverse process (via Zs and wT)
                        controller_store = AttentionStore()
                        register_attention_control(ldm_stable, controller_store)
                        w0, _ = inversion_reverse_process(ldm_stable, xT=wts[skip], etas=eta, prompts=[prompt_dec], cfg_scales=[cfg_scale_dec], prog_bar=True, zs=zs[skip:], controller=controller_store)

                    elif args.mode=="p2pinv":
                        # inversion with attention replace
                        cfg_scale_list = [cfg_scale_enc, cfg_scale_dec]
                        prompts = [prompt_enc, prompt_dec]
                        if enc_dec_len_eq:
                            controller = AttentionReplace(prompts, args.num_diffusion_steps, cross_replace_steps=args.xa, self_replace_steps=args.sa, model=ldm_stable)
                        else:
                        # Should use Refine for target prompts with different number of tokens
                            controller = AttentionRefine(prompts, args.num_diffusion_steps, cross_replace_steps=args.xa, self_replace_steps=args.sa, model=ldm_stable)

                        register_attention_control(ldm_stable, controller)
                        w0, _ = inversion_reverse_process(ldm_stable, xT=wts[skip], etas=eta, prompts=prompts, cfg_scales=cfg_scale_list, prog_bar=True, zs=zs[skip:], controller=controller)
                        w0 = w0[1].unsqueeze(0)
                    elif args.mode=="p2pddim" or args.mode=="ddim":
                        # only z=0
                        if skip != 0:
                            continue
                        prompts = [prompt_enc, prompt_dec]
                        if args.mode=="p2pddim":
                            if enc_dec_len_eq:
                                controller = AttentionReplace(prompts, args.num_diffusion_steps, cross_replace_steps=.8, self_replace_steps=0.4, model=ldm_stable)
                            # Should use Refine for target prompts with different number of tokens
                            else:
                                controller = AttentionRefine(prompts, args.num_diffusion_steps, cross_replace_steps=.8, self_replace_steps=0.4, model=ldm_stable)
                        else:
                            controller = EmptyControl()

                        register_attention_control(ldm_stable, controller)
                        # perform ddim inversion
                        cfg_scale_list = [cfg_scale_enc, cfg_scale_dec]
                        w0, latent = text2image_ldm_stable(ldm_stable, prompts, controller, args.num_diffusion_steps, cfg_scale_list, None, wT)[1]
                    else:
                        raise NotImplementedError
                    
                    # vae decode image
                    with autocast("cuda"), inference_mode():
                        x0_dec = ldm_stable.vae.decode(1 / 0.18215 * w0).sample
                    if x0_dec.dim()<4:
                        x0_dec = x0_dec[None,:,:,:]
                    img = image_grid(x0_dec)
                       
                    # same output
                    current_GMT = time.gmtime()
                    time_stamp_name = calendar.timegm(current_GMT)
                    image_name_png = f'cfg_d_{cfg_scale_dec}_' + f'skip_{skip}_{time_stamp_name}' + ".png"

                    save_full_path = os.path.join(save_path, image_name_png)
                    img.save(save_full_path)

# TODO: Vova:
# (1) check automatically refine or replace
# (2) change model to upload from interent and not local
# (3) requrement file
# TODO: Inbar:
# (1) fix Rene's bug
# (2) write more concisly the inversion_util