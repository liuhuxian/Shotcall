#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务构造：提示词拼装 + ComfyUI API 图（不做任何提交）。"""
import time
import uuid
from pathlib import Path

SIX = ["subject_definitions", "summary", "retention_analysis",
       "detailed_description", "overall_soundscape", "non_diegetic_music"]

MODELS = {
    "unet": "minimax_h3_ref2va_pruned_bf16.safetensors",
    "clip": "qwen3vl_32b_minimax_h3_bf16.safetensors",
    "vae_video": "minimax_h3_video_vae_fp16.safetensors",
    "vae_audio": "minimax_h3_audio_vae_fp32.safetensors",
    "lora_turbo": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
}


def frames_of(sec: float) -> int:
    n = max(5, round(sec * 24))
    return n + (5 - n % 17) % 17


def clean_prompt(six: dict) -> str:
    """拼成 H3 六段式；缺段直接报错，不静默补空。"""
    parts = []
    for k in SIX:
        v = (six.get(k) or "").strip()
        if not v:
            raise ValueError(f"提示词缺少 {k}")
        parts.append(f"{k}: {v}")
    return "\n\n".join(parts)


def build_graph(shot: dict, prompt: str, ref_names: list, frames: int,
                width: int, height: int, steps: int, seed: int, tier: str,
                prefix: str) -> dict:
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODELS["clip"], "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["vae_video"]}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["vae_audio"]}},
        "5": {"class_type": "MiniMaxH3ReferenceToVideo",
              "inputs": {"clip": ["2", 0], "vae": ["3", 0], "audio_vae": ["4", 0],
                         "prompt": prompt, "width": width, "height": height,
                         "length": frames, "ref_image_size": "match"}},
        "6": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "7": {"class_type": "BasicScheduler",
              "inputs": {"model": ["1", 0], "scheduler": "simple", "steps": steps, "denoise": 1.0}},
        "8": {"class_type": "BasicGuider", "inputs": {"model": ["1", 0], "conditioning": ["5", 0]}},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "10": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["9", 0], "guider": ["8", 0], "sampler": ["6", 0],
                          "sigmas": ["7", 0], "latent_image": ["5", 1]}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["10", 0], "vae": ["4", 0]}},
        "13": {"class_type": "CreateVideo",
               "inputs": {"images": ["11", 0], "audio": ["12", 0], "fps": 24}},
        "14": {"class_type": "SaveVideo",
               "inputs": {"video": ["13", 0], "filename_prefix": prefix,
                          "format": "auto", "codec": "auto"}},
    }
    if tier == "draft":
        g["30"] = {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"model": ["1", 0], "lora_name": MODELS["lora_turbo"], "strength_model": 1.0}}
        g["7"]["inputs"]["model"] = ["30", 0]
        g["8"]["inputs"]["model"] = ["30", 0]
    for idx, name in enumerate(ref_names):
        nid = str(20 + idx)
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g["5"]["inputs"][f"ref_images.ref_image_{idx}"] = [nid, 0]
    return g




def plan(data, shot_ids, candidates=3, tier="draft", width=1344, height=768, seed=42, batch=None):
    by_id = {s["id"]: s for s in data["shots"]}
    steps = 4 if tier == "draft" else 20
    tasks = []
    for sid in shot_ids:
        s = by_id[sid]
        six = (s.get("motion") or {}).get("six") or {}
        prompt = clean_prompt(six)
        refs = [r["path"] for r in sorted(s["refs"], key=lambda x: x["order"])]
        frames = frames_of(s["dur"])
        for c in range(candidates):
            tid = "TASK-" + uuid.uuid4().hex[:8]
            tasks.append({
                "id": tid, "shot": sid, "tier": tier, "candidate": c + 1,
                "params": {"width": width, "height": height, "frames": frames,
                           "genSec": round(frames / 24, 2), "steps": steps, "seed": seed + c},
                "batch": batch,
                "refs": refs, "prompt": prompt,
                "graph": build_graph(s, prompt, [Path(r).name for r in refs], frames,
                                     width, height, steps, seed + c, tier,
                                     prefix=f"video/{tid}"),
                "status": "待执行", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            })
    return tasks
