from diffusers import DDIMScheduler, StableDiffusionPipeline

import torch
import torch.nn as nn


class StableDiffusion(nn.Module):
    def __init__(self, args, t_range=[0.02, 0.98]):
        super().__init__()

        self.device = args.device
        self.dtype = args.precision
        print(f'[INFO] loading stable diffusion...')

        model_key = "stabilityai/stable-diffusion-2-1-base"
        pipe = StableDiffusionPipeline.from_pretrained(
            model_key, torch_dtype=self.dtype,
        )

        pipe.to(self.device)
        self.vae = pipe.vae
        self.tokenizer = pipe.tokenizer
        self.text_encoder = pipe.text_encoder
        self.unet = pipe.unet
        self.scheduler = DDIMScheduler.from_pretrained(
            model_key, subfolder="scheduler", torch_dtype=self.dtype,
        )

        del pipe

        self.num_train_timesteps = self.scheduler.config.num_train_timesteps
        self.t_range = t_range
        self.min_step = int(self.num_train_timesteps * t_range[0])
        self.max_step = int(self.num_train_timesteps * t_range[1])
        self.alphas = self.scheduler.alphas_cumprod.to(self.device) # for convenience

        print(f'[INFO] loaded stable diffusion!')

    @torch.no_grad()
    def get_text_embeds(self, prompt):
        inputs = self.tokenizer(prompt, padding='max_length', max_length=self.tokenizer.model_max_length, return_tensors='pt')
        embeddings = self.text_encoder(inputs.input_ids.to(self.device))[0]

        return embeddings
    
    
    def get_noise_preds(self, latents_noisy, t, text_embeddings, guidance_scale=100):
        latent_model_input = torch.cat([latents_noisy] * 2)
            
        tt = torch.cat([t] * 2)
        noise_pred = self.unet(latent_model_input, tt, encoder_hidden_states=text_embeddings).sample

        noise_pred_uncond, noise_pred_pos = noise_pred.chunk(2)
        noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_pos - noise_pred_uncond)
        
        return noise_pred


    def get_sds_loss(
        self, 
        latents,
        text_embeddings, 
        guidance_scale=100, 
        grad_scale=1,
    ):
        # TODO: Implement the loss function for SDS
        t = torch.randint(1, self.num_train_timesteps + 1, (1,), device=self.device)
        zt = torch.randn_like(latents).to(self.device)
        xt = torch.sqrt(self.alphas[t-1]) * latents + torch.sqrt(1 - self.alphas[t-1]) * zt
        noise_pred = self.get_noise_preds(xt, t, text_embeddings, guidance_scale=guidance_scale)
        grad = 2 * (noise_pred - zt)
        target = (latents - grad).detach()

        return nn.functional.mse_loss(latents, target) * grad_scale
    
    def get_pds_loss(
        self, src_latents, tgt_latents, 
        src_text_embedding, tgt_text_embedding,
        guidance_scale=7.5, 
        grad_scale=1,
    ):
        # TODO: Implement the loss function for PDS
        t = torch.randint(self.min_step, self.max_step + 1, (1,), device=self.device)
        zt = torch.randn_like(src_latents)
        zt_prev = torch.randn_like(src_latents)
        alpha_t, alpha_t_prev, beta_t = self.alphas[t], self.alphas[t-1], self.scheduler.betas.to(self.device)[t]
        xt_src = torch.sqrt(alpha_t) * src_latents + torch.sqrt(1 - alpha_t) * zt
        xt_tgt = torch.sqrt(alpha_t) * tgt_latents + torch.sqrt(1 - alpha_t) * zt
        xt_prev_src = torch.sqrt(alpha_t_prev) * src_latents + torch.sqrt(1 - alpha_t_prev) * zt_prev
        xt_prev_tgt = torch.sqrt(alpha_t_prev) * tgt_latents + torch.sqrt(1 - alpha_t_prev) * zt_prev
        sigma2 = (1-alpha_t_prev) * beta_t / (1-alpha_t)
        mu_src = torch.sqrt(alpha_t_prev) * src_latents + torch.sqrt(1 - alpha_t_prev - sigma2) * self.get_noise_preds(xt_src, t, src_text_embedding, guidance_scale=guidance_scale)
        mu_tgt = torch.sqrt(alpha_t_prev) * tgt_latents + torch.sqrt(1 - alpha_t_prev - sigma2) * self.get_noise_preds(xt_tgt, t, tgt_text_embedding, guidance_scale=guidance_scale)
        zt_src = (xt_prev_src - mu_src) / torch.sqrt(sigma2)
        zt_tgt = (xt_prev_tgt - mu_tgt) / torch.sqrt(sigma2)
        grad = 2 * (zt_src - zt_tgt)
        target = (tgt_latents - grad).detach()

        return nn.functional.mse_loss(tgt_latents, target) * grad_scale
    
    
    @torch.no_grad()
    def decode_latents(self, latents):

        latents = 1 / self.vae.config.scaling_factor * latents

        imgs = self.vae.decode(latents).sample
        imgs = (imgs / 2 + 0.5).clamp(0, 1)

        return imgs

    @torch.no_grad()
    def encode_imgs(self, imgs):
        # imgs: [B, 3, H, W]

        imgs = 2 * imgs - 1

        posterior = self.vae.encode(imgs).latent_dist
        latents = posterior.sample() * self.vae.config.scaling_factor

        return latents
