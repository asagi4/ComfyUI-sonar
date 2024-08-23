from __future__ import annotations

import abc
import inspect
import random
from types import SimpleNamespace
from typing import Any, Callable

import numpy as np
import torch
from comfy import samplers

from . import external, noise
from .noise import NoiseType
from .noise_generation import scale_noise
from .sonar import (
    GuidanceConfig,
    GuidanceType,
    HistoryType,
    SonarConfig,
    SonarDPMPPSDE,
    SonarEuler,
    SonarEulerAncestral,
    SonarGuidanceMixin,
)


class NoisyLatentLikeNode:
    DESCRIPTION = "Allows generating noise (and optionally adding it) based on a reference latent. Note: For img2img workflows, you will generally want to enable add_to_latent as well as connecting the model and sigmas inputs."
    RETURN_TYPES = ("LATENT",)
    OUTPUT_TOOLTIPS = ("The noisy latent image.",)
    CATEGORY = "latent/noise"

    FUNCTION = "go"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "noise_type": (
                    tuple(NoiseType.get_names()),
                    {
                        "default": "gaussian",
                        "tooltip": "Sets the type of noise to generate. Has no effect when the custom_noise_opt input is connected.",
                    },
                ),
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 0xFFFFFFFFFFFFFFFF,
                        "tooltip": "Seed to use for generated noise.",
                    },
                ),
                "latent": (
                    "LATENT",
                    {
                        "tooltip": "Latent used as a reference for generating noise.",
                    },
                ),
                "multiplier": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "tooltip": "Multiplier for the strength of the generated noise. Performed after mul_by_sigmas_opt.",
                    },
                ),
                "add_to_latent": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "Add the generated noise to the reference latent rather than adding it to an empty latent. Generally should be enabled for img2img workflows.",
                    },
                ),
                "repeat_batch": (
                    "INT",
                    {
                        "default": 1,
                        "tooltip": "Repeats the noise generation the specified number of times. For example, if set to two and your reference latent is also batch two you will get a batch of four as output.",
                    },
                ),
            },
            "optional": {
                "custom_noise_opt": (
                    "SONAR_CUSTOM_NOISE",
                    {
                        "tooltip": "Allows connecting a custom noise chain. When connected, noise_type has no effect.",
                    },
                ),
                "mul_by_sigmas_opt": (
                    "SIGMAS",
                    {
                        "tooltip": "When connected, will scale the generated noise by the first sigma. Must also connect model_opt to enable.",
                    },
                ),
                "model_opt": (
                    "MODEL",
                    {
                        "tooltip": "Used when mul_by_sigmas_opt is connected, no effect otherwise.",
                    },
                ),
            },
        }

    @classmethod
    def go(
        cls,
        *,
        noise_type: str,
        seed: None | int,
        latent: dict,
        multiplier: float = 1.0,
        add_to_latent=False,
        repeat_batch=1,
        custom_noise_opt: object | None = None,
        mul_by_sigmas_opt: None | torch.Tensor = None,
        model_opt: object | None = None,
    ):
        model, sigmas = model_opt, mul_by_sigmas_opt
        if sigmas is not None and len(sigmas) > 0:
            if model is None:
                raise ValueError(
                    "NoisyLatentLike requires a model when sigmas are connected!",
                )
            while hasattr(model, "model"):
                model = model.model
            latent_scale_factor = model.latent_format.scale_factor
            max_denoise = samplers.Sampler().max_denoise(
                SimpleNamespace(inner_model=model),
                sigmas,
            )
            multiplier *= (
                float(
                    torch.sqrt(1.0 + sigmas[0] ** 2.0) if max_denoise else sigmas[0],
                )
                / latent_scale_factor
            )
        if sigmas is not None and sigmas.numel() > 1:
            sigma_min, sigma_max = sigmas[0], sigmas[-1]
            sigma, sigma_next = sigmas[0], sigmas[1]
        else:
            sigma_min, sigma_max, sigma, sigma_next = (None,) * 4
        latent_samples = latent["samples"]
        if custom_noise_opt is not None:
            ns = custom_noise_opt.make_noise_sampler(
                latent_samples,
                sigma_min=sigma_min,
                sigma_max=sigma_max,
            )
        else:
            ns = noise.get_noise_sampler(
                NoiseType[noise_type.upper()],
                latent_samples,
                sigma_min,
                sigma_max,
                seed=seed,
                cpu=True,
            )
        randst = torch.random.get_rng_state()
        try:
            torch.random.manual_seed(seed)
            result = torch.cat(
                tuple(ns(sigma, sigma_next) for _ in range(repeat_batch)),
                dim=0,
            )
        finally:
            torch.random.set_rng_state(randst)
        result = scale_noise(result, multiplier, normalized=True)
        if add_to_latent:
            result += latent_samples.repeat(
                *(repeat_batch if i == 0 else 1 for i in range(latent_samples.ndim)),
            ).to(result)
        return ({"samples": result},)


class SonarCustomNoiseNodeBase(abc.ABC):
    DESCRIPTION = "A custom noise item."
    RETURN_TYPES = ("SONAR_CUSTOM_NOISE",)
    OUTPUT_TOOLTIPS = ("A custom noise chain.",)
    CATEGORY = "advanced/noise"
    FUNCTION = "go"

    @abc.abstractmethod
    def get_item_class(self):
        raise NotImplementedError

    @classmethod
    def INPUT_TYPES(cls, *, include_rescale=True, include_chain=True):
        result = {
            "required": {
                "factor": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "min": -100.0,
                        "max": 100.0,
                        "step": 0.001,
                        "round": False,
                        "tooltip": "Scaling factor for the generated noise of this type.",
                    },
                ),
            },
            "optional": {},
        }
        if include_rescale:
            result["required"] |= {
                "rescale": (
                    "FLOAT",
                    {
                        "default": 0.0,
                        "min": 0.0,
                        "max": 100.0,
                        "step": 0.001,
                        "round": False,
                        "tooltip": "When non-zero, this custom noise item and other custom noise items items connected to it will have their factor scaled to add up to the specified rescale value. When set to 0, rescaling is disabled.",
                    },
                ),
            }
        if include_chain:
            result["optional"] |= {
                "sonar_custom_noise_opt": (
                    "SONAR_CUSTOM_NOISE",
                    {
                        "tooltip": "Optional input for more custom noise items.",
                    },
                ),
            }
        return result

    def go(
        self,
        factor=1.0,
        rescale=0.0,
        sonar_custom_noise_opt=None,
        **kwargs: dict[str, Any],
    ):
        nis = (
            sonar_custom_noise_opt.clone()
            if sonar_custom_noise_opt
            else noise.CustomNoiseChain()
        )
        if factor != 0:
            nis.add(self.get_item_class()(factor, **kwargs))
        return (nis if rescale == 0 else nis.rescaled(rescale),)


class SonarCustomNoiseNode(SonarCustomNoiseNodeBase):
    @classmethod
    def INPUT_TYPES(cls):
        result = super().INPUT_TYPES()
        result["required"] |= {
            "noise_type": (
                tuple(NoiseType.get_names()),
                {
                    "tooltip": "Sets the type of noise to generate.",
                },
            ),
        }
        return result

    @classmethod
    def get_item_class(cls):
        return noise.CustomNoiseItem


class SonarNormalizeNoiseNodeMixin:
    @staticmethod
    def get_normalize(val: str) -> None | bool:
        return None if val == "default" else val == "forced"


class SonarModulatedNoiseNode(SonarCustomNoiseNodeBase, SonarNormalizeNoiseNodeMixin):
    DESCRIPTION = "Custom noise type that allows modulating the output of another custom noise generator."

    @classmethod
    def INPUT_TYPES(cls):
        result = super().INPUT_TYPES(include_rescale=False, include_chain=False)
        result["required"] |= {
            "sonar_custom_noise": (
                "SONAR_CUSTOM_NOISE",
                {
                    "tooltip": "Input custom noise to modulate.",
                },
            ),
            "modulation_type": (
                (
                    "intensity",
                    "frequency",
                    "spectral_signum",
                    "none",
                ),
                {
                    "tooltip": "Type of modulation to use.",
                },
            ),
            "dims": (
                "INT",
                {
                    "default": 3,
                    "min": 1,
                    "max": 3,
                    "tooltip": "Dimensions to modulate over. 1 - channels only, 2 - height and width, 3 - both",
                },
            ),
            "strength": (
                "FLOAT",
                {
                    "default": 2.0,
                    "min": -100.0,
                    "max": 100.0,
                    "tooltip": "Controls the strength of the modulation effect.",
                },
            ),
            "normalize_result": (
                ("default", "forced", "disabled"),
                {
                    "tooltip": "Controls whether the final result is normalized to 1.0 strength.",
                },
            ),
            "normalize_noise": (
                ("default", "forced", "disabled"),
                {
                    "tooltip": "Controls whether the generated noise is normalized to 1.0 strength.",
                },
            ),
            "normalize_ref": (
                "BOOLEAN",
                {
                    "default": True,
                    "tooltip": "Controls whether the reference latent (when present) is normalized to 1.0 strength.",
                },
            ),
        }
        result["optional"] |= {"ref_latent_opt": ("LATENT",)}
        return result

    @classmethod
    def get_item_class(cls):
        return noise.ModulatedNoise

    def go(
        self,
        *,
        factor,
        sonar_custom_noise,
        modulation_type,
        dims,
        strength,
        normalize_result,
        normalize_noise,
        normalize_ref,
        ref_latent_opt=None,
    ):
        if ref_latent_opt is not None:
            ref_latent_opt = ref_latent_opt["samples"].clone()
        return super().go(
            factor,
            noise=sonar_custom_noise,
            modulation_type=modulation_type,
            modulation_dims=dims,
            modulation_strength=strength,
            normalize_result=self.get_normalize(normalize_result),
            normalize_noise=self.get_normalize(normalize_noise),
            normalize_ref=self.get_normalize(normalize_ref),
            ref_latent_opt=ref_latent_opt,
        )


class SonarRepeatedNoiseNode(SonarCustomNoiseNodeBase, SonarNormalizeNoiseNodeMixin):
    DESCRIPTION = "Custom noise type that allows caching the output of other custom noise generators."

    @classmethod
    def INPUT_TYPES(cls):
        result = super().INPUT_TYPES(include_rescale=False, include_chain=False)
        result["required"] |= {
            "sonar_custom_noise": (
                "SONAR_CUSTOM_NOISE",
                {
                    "tooltip": "Custom noise input for items to repeat. Note: Unlike most other custom noise nodes, this is treated like a list.",
                },
            ),
            "repeat_length": (
                "INT",
                {
                    "default": 8,
                    "min": 1,
                    "max": 100,
                    "tooltip": "Number of items to cache.",
                },
            ),
            "max_recycle": (
                "INT",
                {
                    "default": 1000,
                    "min": 1,
                    "max": 1000,
                    "tooltip": "Number of times an individual item will be used before it is replaced with fresh noise.",
                },
            ),
            "normalize": (
                ("default", "forced", "disabled"),
                {
                    "tooltip": "Controls whether the generated noise is normalized to 1.0 strength.",
                },
            ),
            "permute": (
                ("enabled", "disabled", "always"),
                {
                    "tooltip": "When enabled, recycled noise will be permuted by randomly flipping it, rolling the channels, etc. If set to always, the noise will be permuted the first time it's used as well.",
                },
            ),
        }
        return result

    @classmethod
    def get_item_class(cls):
        return noise.RepeatedNoise

    def go(
        self,
        *,
        factor,
        sonar_custom_noise,
        repeat_length,
        max_recycle,
        normalize,
        permute=True,
    ):
        return super().go(
            factor,
            noise=sonar_custom_noise,
            repeat_length=repeat_length,
            max_recycle=max_recycle,
            normalize=self.get_normalize(normalize),
            permute=permute,
        )


class SonarScheduledNoiseNode(SonarCustomNoiseNodeBase, SonarNormalizeNoiseNodeMixin):
    DESCRIPTION = "Custom noise type that allows scheduling the output of other custom noise generators. NOTE: If you don't connect the fallback custom noise input, no noise will be generated outside of the start_percent, end_percent range. Recommend connecting a 1.0 strength Gaussian custom noise node as the fallback."

    @classmethod
    def INPUT_TYPES(cls):
        result = super().INPUT_TYPES(include_rescale=False, include_chain=False)
        result["required"] |= {
            "model": (
                "MODEL",
                {
                    "tooltip": "The model input is required to calculate sampling percentages.",
                },
            ),
            "sonar_custom_noise": (
                "SONAR_CUSTOM_NOISE",
                {
                    "tooltip": "Custom noise to use when start_percent and end_percent matches.",
                },
            ),
            "start_percent": (
                "FLOAT",
                {
                    "default": 0.0,
                    "min": 0.0,
                    "max": 1.0,
                    "tooltip": "Time the custom noise becomes active. Note: Sampling percentage where 1.0 indicates 100%, not based on steps.",
                },
            ),
            "end_percent": (
                "FLOAT",
                {
                    "default": 1.0,
                    "min": 0.0,
                    "max": 1.0,
                    "tooltip": "Time the custom noise effect ends - inclusive, so only sampling percentages greater than this will be excluded. Note: Sampling percentage where 1.0 indicates 100%, not based on steps.",
                },
            ),
            "normalize": (
                ("default", "forced", "disabled"),
                {
                    "tooltip": "Controls whether the generated noise is normalized to 1.0 strength.",
                },
            ),
        }
        result["optional"] |= {
            "fallback_sonar_custom_noise": (
                "SONAR_CUSTOM_NOISE",
                {
                    "tooltip": "Optional input for noise to use when outside of the start_percent, end_percent range. NOTE: When not connected, defaults to NO NOISE which is probably not what you want.",
                },
            ),
        }
        return result

    @classmethod
    def get_item_class(cls):
        return noise.ScheduledNoise

    def go(
        self,
        *,
        model,
        factor,
        sonar_custom_noise,
        start_percent,
        end_percent,
        normalize,
        fallback_sonar_custom_noise=None,
    ):
        ms = model.get_model_object("model_sampling")
        start_sigma = ms.percent_to_sigma(start_percent)
        end_sigma = ms.percent_to_sigma(end_percent)
        return super().go(
            factor,
            noise=sonar_custom_noise,
            start_sigma=start_sigma,
            end_sigma=end_sigma,
            normalize=self.get_normalize(normalize),
            fallback_noise=fallback_sonar_custom_noise,
        )


class SonarCompositeNoiseNode(SonarCustomNoiseNodeBase, SonarNormalizeNoiseNodeMixin):
    DESCRIPTION = "Custom noise type that allows compositing two other custom noise generators based on a mask."

    @classmethod
    def INPUT_TYPES(cls):
        result = super().INPUT_TYPES(include_rescale=False, include_chain=False)
        result["required"] |= {
            "sonar_custom_noise_dst": (
                "SONAR_CUSTOM_NOISE",
                {
                    "tooltip": "Custom noise input for noise where the mask is not set.",
                },
            ),
            "sonar_custom_noise_src": (
                "SONAR_CUSTOM_NOISE",
                {
                    "tooltip": "Custom noise input for noise where the mask is set..",
                },
            ),
            "normalize_dst": (
                ("default", "forced", "disabled"),
                {
                    "tooltip": "Controls whether noise generated for dst is normalized to 1.0 strength.",
                },
            ),
            "normalize_src": (
                ("default", "forced", "disabled"),
                {
                    "tooltip": "Controls whether noise generated for src is normalized to 1.0 strength.",
                },
            ),
            "normalize_result": (
                ("default", "forced", "disabled"),
                {
                    "tooltip": "Controls whether the final result after composition is normalized to 1.0 strength.",
                },
            ),
            "mask": (
                "MASK",
                {
                    "tooltip": "Mask to use when compositing noise. Where the mask is 1.0, you will get 100% src, where it is 0.75 you will get 75% src and 25% dst. The mask will be rescaled to match the latent size if necessary.",
                },
            ),
        }
        return result

    @classmethod
    def get_item_class(cls):
        return noise.CompositeNoise

    def go(
        self,
        *,
        factor,
        sonar_custom_noise_dst,
        sonar_custom_noise_src,
        normalize_src,
        normalize_dst,
        normalize_result,
        mask,
    ):
        return super().go(
            factor,
            dst_noise=sonar_custom_noise_dst,
            src_noise=sonar_custom_noise_src,
            normalize_dst=self.get_normalize(normalize_src),
            normalize_src=self.get_normalize(normalize_dst),
            normalize_result=self.get_normalize(normalize_result),
            mask=mask,
        )


class SonarGuidedNoiseNode(SonarCustomNoiseNodeBase, SonarNormalizeNoiseNodeMixin):
    DESCRIPTION = "Custom noise type that mixes a references with another custom noise generator to guide the generation."

    @classmethod
    def INPUT_TYPES(cls):
        result = super().INPUT_TYPES(include_rescale=False, include_chain=False)
        result["required"] |= {
            "latent": (
                "LATENT",
                {
                    "tooltip": "Latent to use for guidance.",
                },
            ),
            "sonar_custom_noise": (
                "SONAR_CUSTOM_NOISE",
                {
                    "tooltip": "Custom noise input to combine with the guidance.",
                },
            ),
            "method": (
                ("euler", "linear"),
                {
                    "tooltip": "Method to use when calculating guidance. When set to linear, will simply LERP the guidance at the specified strength. When set to Euler, will do a Euler step toward the guidance instead.",
                },
            ),
            "guidance_factor": (
                "FLOAT",
                {
                    "default": 0.0125,
                    "min": -100.0,
                    "max": 100.0,
                    "step": 0.001,
                    "round": False,
                    "tooltip": "Strength of the guidance to apply. Generally should be a relatively slow value to avoid overpowering the generation.",
                },
            ),
            "normalize_noise": (
                ("default", "forced", "disabled"),
                {
                    "tooltip": "Controls whether the generated noise is normalized to 1.0 strength.",
                },
            ),
            "normalize_result": (
                ("default", "forced", "disabled"),
                {
                    "tooltip": "Controls whether the final result is normalized to 1.0 strength.",
                },
            ),
            "normalize_ref": (
                "BOOLEAN",
                {
                    "default": True,
                    "tooltip": "Controls whether the reference latent (when present) is normalized to 1.0 strength.",
                },
            ),
        }
        return result

    @classmethod
    def get_item_class(cls):
        return noise.GuidedNoise

    def go(
        self,
        *,
        factor,
        latent,
        sonar_custom_noise,
        normalize_noise,
        normalize_result,
        normalize_ref=True,
        method="euler",
        guidance_factor=0.5,
    ):
        return super().go(
            factor,
            ref_latent=scale_noise(
                SonarGuidanceMixin.prepare_ref_latent(latent["samples"].clone()),
                normalized=normalize_ref,
            ),
            guidance_factor=guidance_factor,
            noise=sonar_custom_noise.clone(),
            method=method,
            normalize_noise=self.get_normalize(normalize_noise),
            normalize_result=self.get_normalize(normalize_result),
        )


class SonarRandomNoiseNode(SonarCustomNoiseNodeBase, SonarNormalizeNoiseNodeMixin):
    DESCRIPTION = "Custom noise type that randomly selects between other custom noise items connected to it."

    @classmethod
    def INPUT_TYPES(cls):
        result = super().INPUT_TYPES(include_rescale=False, include_chain=False)
        result["required"] |= {
            "sonar_custom_noise": (
                "SONAR_CUSTOM_NOISE",
                {
                    "tooltip": "Custom noise input for noise items to randomize. Note: Unlike most other custom noise nodes, this is treated like a list.",
                },
            ),
            "mix_count": (
                "INT",
                {
                    "default": 1,
                    "min": 1,
                    "max": 100,
                    "tooltip": "Number of items to select each time noise is generated.",
                },
            ),
            "normalize": (
                ("default", "forced", "disabled"),
                {
                    "tooltip": "Controls whether the generated noise is normalized to 1.0 strength.",
                },
            ),
        }

        return result

    @classmethod
    def get_item_class(cls):
        return noise.RandomNoise

    def go(
        self,
        factor,
        sonar_custom_noise,
        mix_count,
        normalize,
    ):
        return super().go(
            factor,
            noise=sonar_custom_noise,
            mix_count=mix_count,
            normalize=self.get_normalize(normalize),
        )


class CustomNOISE:
    def __init__(
        self,
        custom_noise,
        seed,
        *,
        cpu_noise=True,
        normalize=True,
        multiplier=1.0,
    ):
        self.custom_noise = custom_noise
        self.seed = seed
        self.cpu_noise = cpu_noise
        self.normalize = normalize
        self.multiplier = multiplier

    def _sample_noise(self, latent_image, seed):
        result = self.custom_noise.make_noise_sampler(
            latent_image,
            None,
            None,
            seed=seed,
            cpu=self.cpu_noise,
            normalized=self.normalize,
        )(None, None).to(
            device="cpu",
            dtype=latent_image.dtype,
        )
        if result.layout != latent_image.layout:
            if latent_image.layout == torch.sparse_coo:
                return result.to_sparse()
            errstr = f"Cannot handle latent layout {type(latent_image.layout).__name__}"
            raise NotImplementedError(errstr)
        return result if self.multiplier == 1.0 else result.mul_(self.multiplier)

    def generate_noise(self, input_latent):
        latent_image = input_latent["samples"]
        batch_inds = input_latent.get("batch_index")
        torch.manual_seed(self.seed)
        random.seed(self.seed)
        if self.multiplier == 0.0:
            return torch.zeros(
                latent_image.shape,
                dtype=latent_image.dtype,
                layout=latent_image.layout,
                device="cpu",
            )
        if batch_inds is None:
            return self._sample_noise(latent_image, self.seed)
        unique_inds, inverse_inds = np.unique(batch_inds, return_inverse=True)
        result = []
        batch_size = latent_image.shape[0]
        for idx in range(unique_inds[-1] + 1):
            noise = self._sample_noise(
                latent_image[idx % batch_size].unsqueeze(0),
                self.seed + idx,
            )
            if idx in unique_inds:
                result.append(noise)
        return torch.cat(tuple(result[i] for i in inverse_inds), axis=0)


class SonarToComfyNOISENode:
    DESCRIPTION = "Allows converting SONAR_CUSTOM_NOISE to NOISE (used by SamplerCustomAdvanced and possibly other custom samplers). NOTE: Does not work with noise types that depend on sigma (Brownian, ScheduledNoise, etc)."
    RETURN_TYPES = ("NOISE",)
    CATEGORY = "sampling/custom_sampling/noise"
    FUNCTION = "go"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "custom_noise": (
                    "SONAR_CUSTOM_NOISE",
                    {
                        "tooltip": "Custom noise type to convert.",
                    },
                ),
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 0xFFFFFFFFFFFFFFFF,
                        "tooltip": "Seed to use for generated noise.",
                    },
                ),
                "cpu_noise": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "Controls whether noise is generated on CPU or GPU.",
                    },
                ),
                "normalize": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "Controls whether generated noise is normalized to 1.0 strength.",
                    },
                ),
                "multiplier": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "step": 0.001,
                        "round": False,
                        "tooltip": "Simple multiplier applied to noise after all other scaling and normalization effects. If set to 0, no noise will be generated (same as disabling noise).",
                    },
                ),
            },
        }

    @classmethod
    def go(cls, *, custom_noise, seed, cpu_noise=True, normalize=True, multiplier=1.0):
        return (
            CustomNOISE(
                custom_noise,
                seed,
                cpu_noise=cpu_noise,
                normalize=normalize,
                multiplier=multiplier,
            ),
        )


class GuidanceConfigNode:
    DESCRIPTION = "Allows specifying extended guidance parameters for Sonar samplers."

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "factor": (
                    "FLOAT",
                    {
                        "default": 0.01,
                        "min": -2.0,
                        "max": 2.0,
                        "step": 0.001,
                        "round": False,
                        "tooltip": "Controls the strength of the guidance. You'll generally want to use fairly low values here.",
                    },
                ),
                "guidance_type": (
                    tuple(t.name.lower() for t in GuidanceType),
                    {
                        "tooltip": "Method to use when calculating guidance. When set to linear, will simply LERP the guidance at the specified strength. When set to Euler, will do a Euler step toward the guidance instead.",
                    },
                ),
                "start_step": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "tooltip": "First zero-based step the guidance is active.",
                    },
                ),
                "end_step": (
                    "INT",
                    {
                        "default": 9999,
                        "min": 0,
                        "tooltip": "Last zero-based step the guidance is active.",
                    },
                ),
                "latent": (
                    "LATENT",
                    {"tooltip": "Latent to use as a reference for guidance."},
                ),
            },
        }

    RETURN_TYPES = ("SONAR_GUIDANCE_CFG",)
    CATEGORY = "sampling/custom_sampling/samplers"

    FUNCTION = "make_guidance_cfg"

    @classmethod
    def make_guidance_cfg(
        cls,
        guidance_type,
        factor,
        start_step,
        end_step,
        latent,
    ):
        return (
            GuidanceConfig(
                guidance_type=GuidanceType[guidance_type.upper()],
                factor=factor,
                start_step=start_step,
                end_step=end_step,
                latent=latent.get("samples"),
            ),
        )


class SamplerNodeSonarBase:
    DESCRIPTION = "Sonar - momentum based sampler node."

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "momentum": (
                    "FLOAT",
                    {
                        "default": 0.95,
                        "min": -0.5,
                        "max": 2.5,
                        "step": 0.01,
                        "round": False,
                        "tooltip": "Strength of the output from normal sampling. When set to 1.0 effectively disables momentum.",
                    },
                ),
                "momentum_hist": (
                    "FLOAT",
                    {
                        "default": 0.75,
                        "min": -1.5,
                        "max": 1.5,
                        "step": 0.01,
                        "round": False,
                        "tooltip": "Strength of momentum history",
                    },
                ),
                "momentum_init": (
                    tuple(t.name for t in HistoryType),
                    {
                        "tooltip": "Initial value used for momentum history. ZERO - history starts zeroed out. RAND - History is initialized with a random value. SAMPLE - History is initialized from the latent at the start of sampling.",
                    },
                ),
                "direction": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "min": -30.0,
                        "max": 15.0,
                        "step": 0.01,
                        "round": False,
                        "tooltip": "Multiplier applied to the result of normal sampling.",
                    },
                ),
                "rand_init_noise_type": (
                    tuple(NoiseType.get_names(skip=(NoiseType.BROWNIAN,))),
                    {
                        "tooltip": "Noise type to use when momentum_init is set to RANDOM.",
                    },
                ),
            },
            "optional": {
                "guidance_cfg_opt": (
                    "SONAR_GUIDANCE_CFG",
                    {
                        "tooltip": "Optional input for extended guidance parameters.",
                    },
                ),
            },
        }

    RETURN_TYPES = ("SAMPLER",)
    CATEGORY = "sampling/custom_sampling/samplers"


class SamplerNodeSonarEuler(SamplerNodeSonarBase):
    @classmethod
    def INPUT_TYPES(cls):
        result = super().INPUT_TYPES()
        result["required"].update(
            {
                "s_noise": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "min": 0.0,
                        "max": 100.0,
                        "step": 0.01,
                        "round": False,
                        "tooltip": "Multiplier for noise added during ancestral or SDE sampling.",
                    },
                ),
            },
        )
        return result

    RETURN_TYPES = ("SAMPLER",)
    CATEGORY = "sampling/custom_sampling/samplers"

    FUNCTION = "get_sampler"

    @classmethod
    def get_sampler(
        cls,
        *,
        momentum,
        momentum_hist,
        momentum_init,
        direction,
        rand_init_noise_type,
        s_noise,
        guidance_cfg_opt=None,
    ):
        cfg = SonarConfig(
            momentum=momentum,
            init=HistoryType[momentum_init.upper()],
            momentum_hist=momentum_hist,
            direction=direction,
            rand_init_noise_type=NoiseType[rand_init_noise_type.upper()],
            guidance=guidance_cfg_opt,
        )
        return (
            samplers.KSAMPLER(
                SonarEuler.sampler,
                {
                    "s_noise": s_noise,
                    "sonar_config": cfg,
                },
            ),
        )


class SamplerNodeSonarEulerAncestral(SamplerNodeSonarEuler):
    @classmethod
    def INPUT_TYPES(cls):
        result = super().INPUT_TYPES()
        result["required"].update(
            {
                "eta": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "min": 0.0,
                        "max": 100.0,
                        "step": 0.01,
                        "round": False,
                        "tooltip": "Basically controls the ancestralness of the sampler. When set to 0, you will get a non-ancestral (or SDE) sampler.",
                    },
                ),
                "noise_type": (
                    tuple(NoiseType.get_names()),
                    {
                        "tooltip": "Noise type used during ancestral or SDE sampling. Only used when the custom noise input is not connected.",
                    },
                ),
            },
        )
        result["optional"].update(
            {
                "custom_noise_opt": (
                    "SONAR_CUSTOM_NOISE",
                    {
                        "tooltip": "Optional input for custom noise used during ancestral or SDE sampling. When connected, the built-in noise_type selector is ignored.",
                    },
                ),
            },
        )
        return result

    @classmethod
    def get_sampler(
        cls,
        *,
        momentum,
        momentum_hist,
        momentum_init,
        direction,
        rand_init_noise_type,
        noise_type,
        eta,
        s_noise,
        guidance_cfg_opt=None,
        custom_noise_opt=None,
    ):
        cfg = SonarConfig(
            momentum=momentum,
            init=HistoryType[momentum_init.upper()],
            momentum_hist=momentum_hist,
            direction=direction,
            rand_init_noise_type=NoiseType[rand_init_noise_type.upper()],
            noise_type=NoiseType[noise_type.upper()],
            custom_noise=custom_noise_opt.clone() if custom_noise_opt else None,
            guidance=guidance_cfg_opt,
        )
        return (
            samplers.KSAMPLER(
                SonarEulerAncestral.sampler,
                {
                    "sonar_config": cfg,
                    "eta": eta,
                    "s_noise": s_noise,
                },
            ),
        )


class SamplerNodeSonarDPMPPSDE(SamplerNodeSonarEuler):
    @classmethod
    def INPUT_TYPES(cls):
        result = super().INPUT_TYPES()
        result["required"].update(
            {
                "eta": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "min": 0.0,
                        "max": 100.0,
                        "step": 0.01,
                        "round": False,
                        "tooltip": "Basically controls the ancestralness of the sampler. When set to 0, you will get a non-ancestral (or SDE) sampler.",
                    },
                ),
                "noise_type": (
                    tuple(NoiseType.get_names(default=NoiseType.BROWNIAN)),
                    {
                        "tooltip": "Noise type used during ancestral or SDE sampling. Only used when the custom noise input is not connected.",
                    },
                ),
            },
        )
        result["optional"].update(
            {
                "custom_noise_opt": (
                    "SONAR_CUSTOM_NOISE",
                    {
                        "tooltip": "Optional input for custom noise used during ancestral or SDE sampling. When connected, the built-in noise_type selector is ignored.",
                    },
                ),
            },
        )
        return result

    @classmethod
    def get_sampler(
        cls,
        *,
        momentum,
        momentum_hist,
        momentum_init,
        direction,
        rand_init_noise_type,
        noise_type,
        eta,
        s_noise,
        guidance_cfg_opt=None,
        custom_noise_opt=None,
    ):
        cfg = SonarConfig(
            momentum=momentum,
            init=HistoryType[momentum_init.upper()],
            momentum_hist=momentum_hist,
            direction=direction,
            rand_init_noise_type=NoiseType[rand_init_noise_type.upper()],
            noise_type=NoiseType[noise_type.upper()],
            custom_noise=custom_noise_opt.clone() if custom_noise_opt else None,
            guidance=guidance_cfg_opt,
        )
        return (
            samplers.KSAMPLER(
                SonarDPMPPSDE.sampler,
                {
                    "sonar_config": cfg,
                    "eta": eta,
                    "s_noise": s_noise,
                },
            ),
        )


class SamplerNodeConfigOverride:
    DESCRIPTION = "Allows overriding paramaters for a SAMPLER. Only parameters that particular sampler supports will be applied, so for example setting ETA will have no effect for non-ancestral Euler."
    KWARG_OVERRIDES = ("s_noise", "eta", "s_churn", "r", "solver_type")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "sampler": ("SAMPLER",),
                "eta": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "step": 0.01,
                        "round": False,
                        "tooltip": "Basically controls the ancestralness of the sampler. When set to 0, you will get a non-ancestral (or SDE) sampler.",
                    },
                ),
                "s_noise": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "step": 0.01,
                        "round": False,
                        "tooltip": "Multiplier for noise added during ancestral or SDE sampling.",
                    },
                ),
                "s_churn": (
                    "FLOAT",
                    {
                        "default": 0.0,
                        "min": 0.0,
                        "step": 0.01,
                        "round": False,
                        "tooltip": "Churn was the predececessor of ETA. Only used by a few types of samplers (notably Euler non-ancestral). Not used by any ancestral or SDE samplers.",
                    },
                ),
                "r": (
                    "FLOAT",
                    {
                        "default": 0.5,
                        "step": 0.01,
                        "round": False,
                        "tooltip": "Used by dpmpp_sde.",
                    },
                ),
                "sde_solver": (
                    ("midpoint", "heun"),
                    {
                        "tooltip": "Solver used by dpmpp_2m_sde.",
                    },
                ),
                "cpu_noise": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "Controls whether noise is generated on CPU or GPU.",
                    },
                ),
                "normalize": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "Controls whether generated noise is normalized to 1.0 strength.",
                    },
                ),
            },
            "optional": {
                "noise_type": (
                    tuple(NoiseType.get_names()),
                    {
                        "tooltip": "Noise type used during ancestral or SDE sampling. Only used when the custom noise input is not connected.",
                    },
                ),
                "custom_noise_opt": (
                    "SONAR_CUSTOM_NOISE",
                    {
                        "tooltip": "Optional input for custom noise used during ancestral or SDE sampling. When connected, the built-in noise_type selector is ignored.",
                    },
                ),
            },
        }

    RETURN_TYPES = ("SAMPLER",)
    CATEGORY = "sampling/custom_sampling/samplers"

    FUNCTION = "get_sampler"

    def get_sampler(
        self,
        *,
        sampler,
        eta,
        s_noise,
        s_churn,
        r,
        sde_solver,
        cpu_noise=True,
        noise_type=None,
        custom_noise_opt=None,
        normalize=True,
    ):
        return (
            samplers.KSAMPLER(
                self.sampler_function,
                extra_options=sampler.extra_options
                | {
                    "override_sampler_cfg": {
                        "sampler": sampler,
                        "noise_type": NoiseType[noise_type.upper()]
                        if noise_type is not None
                        else None,
                        "custom_noise": custom_noise_opt,
                        "s_noise": s_noise,
                        "eta": eta,
                        "s_churn": s_churn,
                        "r": r,
                        "solver_type": sde_solver,
                        "cpu_noise": cpu_noise,
                        "normalize": normalize,
                    },
                },
                inpaint_options=sampler.inpaint_options | {},
            ),
        )

    @classmethod
    @torch.no_grad()
    def sampler_function(
        cls,
        model,
        x,
        sigmas,
        *args: list[Any],
        override_sampler_cfg: dict[str, Any] | None = None,
        noise_sampler: Callable | None = None,
        extra_args: dict[str, Any] | None = None,
        **kwargs: dict[str, Any],
    ):
        if not override_sampler_cfg:
            raise ValueError("Override sampler config missing!")
        if extra_args is None:
            extra_args = {}
        cfg = override_sampler_cfg
        sampler, noise_type, custom_noise, cpu, normalize = (
            cfg["sampler"],
            cfg.get("noise_type"),
            cfg.get("custom_noise"),
            cfg.get("cpu_noise", True),
            cfg.get("normalize", True),
        )
        sigma_min, sigma_max = sigmas[sigmas > 0].min(), sigmas.max()
        seed = extra_args.get("seed")
        if custom_noise is not None:
            noise_sampler = custom_noise.make_noise_sampler(
                x,
                sigma_min,
                sigma_max,
                seed=seed,
                cpu=cpu,
                normalized=normalize,
            )
        elif noise_type is not None:
            noise_sampler = noise.get_noise_sampler(
                noise_type,
                x,
                sigma_min,
                sigma_max,
                seed=seed,
                cpu=cpu,
                normalized=normalize,
            )
        sig = inspect.signature(sampler.sampler_function)
        params = sig.parameters
        kwargs = kwargs.copy()
        if "noise_sampler" in params:
            kwargs["noise_sampler"] = noise_sampler
        for k in cls.KWARG_OVERRIDES:
            if k not in params or cfg.get(k) is None:
                continue
            kwargs[k] = cfg[k]
        return sampler.sampler_function(
            model,
            x,
            sigmas,
            *args,
            extra_args=extra_args,
            **kwargs,
        )


NODE_CLASS_MAPPINGS = {
    "SamplerSonarEuler": SamplerNodeSonarEuler,
    "SamplerSonarEulerA": SamplerNodeSonarEulerAncestral,
    "SamplerSonarDPMPPSDE": SamplerNodeSonarDPMPPSDE,
    "SonarGuidanceConfig": GuidanceConfigNode,
    "SamplerConfigOverride": SamplerNodeConfigOverride,
    "NoisyLatentLike": NoisyLatentLikeNode,
    "SonarCustomNoise": SonarCustomNoiseNode,
    "SonarCompositeNoise": SonarCompositeNoiseNode,
    "SonarModulatedNoise": SonarModulatedNoiseNode,
    "SonarRepeatedNoise": SonarRepeatedNoiseNode,
    "SonarScheduledNoise": SonarScheduledNoiseNode,
    "SonarGuidedNoise": SonarGuidedNoiseNode,
    "SonarRandomNoise": SonarRandomNoiseNode,
    "SONAR_CUSTOM_NOISE to NOISE": SonarToComfyNOISENode,
}

NODE_DISPLAY_NAME_MAPPINGS = {}


if "bleh" in external.MODULES:
    import ast

    bleh = external.MODULES["bleh"]
    bleh_latentutils = bleh.py.latent_utils

    class SonarBlendFilterNoiseNode(
        SonarCustomNoiseNodeBase,
        SonarNormalizeNoiseNodeMixin,
    ):
        DESCRIPTION = "Custom noise type that allows blending and filtering the output of another noise generator."

        @classmethod
        def INPUT_TYPES(cls):
            result = super().INPUT_TYPES(include_rescale=False, include_chain=False)
            result["required"] |= {
                "sonar_custom_noise": ("SONAR_CUSTOM_NOISE",),
                "blend_mode": (
                    ("simple_add", *bleh_latentutils.BLENDING_MODES.keys()),
                ),
                "ffilter": (tuple(bleh_latentutils.FILTER_PRESETS.keys()),),
                "ffilter_custom": ("STRING", {"default": ""}),
                "ffilter_scale": (
                    "FLOAT",
                    {"default": 1.0, "min": -100.0, "max": 100.0},
                ),
                "ffilter_strength": (
                    "FLOAT",
                    {"default": 0.0, "min": -100.0, "max": 100.0},
                ),
                "ffilter_threshold": (
                    "INT",
                    {"default": 1, "min": 1, "max": 32},
                ),
                "enhance_mode": (("none", *bleh_latentutils.ENHANCE_METHODS),),
                "enhance_strength": (
                    "FLOAT",
                    {"default": 0.0, "min": -100.0, "max": 100.0},
                ),
                "affect": (("result", "noise", "both"),),
                "normalize_result": (("default", "forced", "disabled"),),
                "normalize_noise": (("default", "forced", "disabled"),),
            }
            return result

        @classmethod
        def get_item_class(cls):
            return noise.BlendFilterNoise

        def go(
            self,
            *,
            factor,
            sonar_custom_noise,
            blend_mode,
            ffilter,
            ffilter_custom,
            ffilter_scale,
            ffilter_strength,
            ffilter_threshold,
            enhance_mode,
            enhance_strength,
            affect,
            normalize_result,
            normalize_noise,
        ):
            ffilter_custom = ffilter_custom.strip()
            normalize_result = (
                None if normalize_result == "default" else normalize_result == "forced"
            )
            normalize_noise = (
                None if normalize_noise == "default" else normalize_noise == "forced"
            )
            if ffilter_custom:
                ffilter = ast.literal_eval(f"[{ffilter_custom}]")
            else:
                ffilter = bleh_latentutils.FILTER_PRESETS[ffilter]
            return super().go(
                factor,
                noise=sonar_custom_noise.clone(),
                blend_mode=blend_mode,
                ffilter=ffilter,
                ffilter_scale=ffilter_scale,
                ffilter_strength=ffilter_strength,
                ffilter_threshold=ffilter_threshold,
                enhance_mode=enhance_mode,
                enhance_strength=enhance_strength,
                affect=affect,
                normalize_noise=self.get_normalize(normalize_noise),
                normalize_result=self.get_normalize(normalize_result),
            )

    NODE_CLASS_MAPPINGS["SonarBlendFilterNoise"] = SonarBlendFilterNoiseNode

if "restart" in external.MODULES:
    rs = external.MODULES["restart"]

    class KRestartSamplerCustomNoise:
        DESCRIPTION = "Restart sampler variant that allows specifying a custom noise type for noise added by restarts."

        @classmethod
        def INPUT_TYPES(cls):
            get_normal_schedulers = getattr(
                rs.nodes,
                "get_supported_normal_schedulers",
                rs.nodes.get_supported_restart_schedulers,
            )
            return {
                "required": {
                    "model": ("MODEL",),
                    "add_noise": (["enable", "disable"],),
                    "noise_seed": (
                        "INT",
                        {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF},
                    ),
                    "steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
                    "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}),
                    "sampler": ("SAMPLER",),
                    "scheduler": (get_normal_schedulers(),),
                    "positive": ("CONDITIONING",),
                    "negative": ("CONDITIONING",),
                    "latent_image": ("LATENT",),
                    "start_at_step": ("INT", {"default": 0, "min": 0, "max": 10000}),
                    "end_at_step": ("INT", {"default": 10000, "min": 0, "max": 10000}),
                    "return_with_leftover_noise": (["disable", "enable"],),
                    "segments": (
                        "STRING",
                        {
                            "default": rs.restart_sampling.DEFAULT_SEGMENTS,
                            "multiline": False,
                        },
                    ),
                    "restart_scheduler": (rs.nodes.get_supported_restart_schedulers(),),
                    "chunked_mode": ("BOOLEAN", {"default": True}),
                },
                "optional": {
                    "custom_noise_opt": ("SONAR_CUSTOM_NOISE",),
                },
            }

        RETURN_TYPES = ("LATENT", "LATENT")
        RETURN_NAMES = ("output", "denoised_output")
        FUNCTION = "sample"
        CATEGORY = "sampling"

        @classmethod
        def sample(
            cls,
            *,
            model,
            add_noise,
            noise_seed,
            steps,
            cfg,
            sampler,
            scheduler,
            positive,
            negative,
            latent_image,
            start_at_step,
            end_at_step,
            return_with_leftover_noise,
            segments,
            restart_scheduler,
            chunked_mode=False,
            custom_noise_opt=None,
        ):
            return rs.restart_sampling.restart_sampling(
                model,
                noise_seed,
                steps,
                cfg,
                sampler,
                scheduler,
                positive,
                negative,
                latent_image,
                segments,
                restart_scheduler,
                disable_noise=add_noise == "disable",
                step_range=(start_at_step, end_at_step),
                force_full_denoise=return_with_leftover_noise != "enable",
                output_only=False,
                chunked_mode=chunked_mode,
                custom_noise=custom_noise_opt.make_noise_sampler
                if custom_noise_opt
                else None,
            )

    NODE_CLASS_MAPPINGS["KRestartSamplerCustomNoise"] = KRestartSamplerCustomNoise

    if hasattr(rs.restart_sampling, "RestartSampler"):

        class RestartSamplerCustomNoise:
            DESCRIPTION = "Wrapper used to make another sampler Restart compatible. Allows specifying a custom type for noise added by restarts."

            @classmethod
            def INPUT_TYPES(cls):
                return {
                    "required": {
                        "sampler": ("SAMPLER",),
                        "chunked_mode": ("BOOLEAN", {"default": True}),
                    },
                    "optional": {
                        "custom_noise_opt": ("SONAR_CUSTOM_NOISE",),
                    },
                }

            RETURN_TYPES = ("SAMPLER",)
            FUNCTION = "go"
            CATEGORY = "sampling/custom_sampling/samplers"

            @classmethod
            def go(cls, sampler, chunked_mode, custom_noise_opt=None):
                restart_options = {
                    "restart_chunked": chunked_mode,
                    "restart_wrapped_sampler": sampler,
                    "restart_custom_noise": None
                    if custom_noise_opt is None
                    else custom_noise_opt.make_noise_sampler,
                }
                restart_sampler = samplers.KSAMPLER(
                    rs.restart_sampling.RestartSampler.sampler_function,
                    extra_options=sampler.extra_options | restart_options,
                    inpaint_options=sampler.inpaint_options,
                )
                return (restart_sampler,)

        NODE_CLASS_MAPPINGS["RestartSamplerCustomNoise"] = RestartSamplerCustomNoise
