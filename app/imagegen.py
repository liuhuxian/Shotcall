# -*- coding: utf-8 -*-
"""H3 Image 出图：提示词解析、图构造、预检。

图结构来自本机实测可用的 ComfyUI 工作流（test_h3_image.py 的 t2i / edit 分支）。
"""
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

UNET = "minimax_h3_image_pruned_bf16.safetensors"
CLIP = "qwen3vl_32b_minimax_h3_bf16.safetensors"
VAE = "minimax_h3_image_vae_fp16.safetensors"

DEFAULT_W, DEFAULT_H = 2048, 1152      # 16:9 · 4MP 区间，均为 32 倍数
DEFAULT_STEPS = 6


def _post(port, path, payload, timeout=60):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def build_graph(prompt: str, width=DEFAULT_W, height=DEFAULT_H, seed=42,
                ref_image: str = None, steps=DEFAULT_STEPS) -> dict:
    """文生图；给了 ref_image 就是参考图改图（image_1 为要改的图）。"""
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "5": {"class_type": "MiniMaxH3ImageConditioning",
              "inputs": {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt,
                         "width": width, "height": height}},
        "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["5", 0]}},
        "50": {"class_type": "ModelAttentionBackend",
               "inputs": {"model": ["1", 0], "attention": "pytorch attention"}},
        "7": {"class_type": "KSampler",
              "inputs": {"model": ["50", 0], "positive": ["5", 0], "negative": ["6", 0],
                         "latent_image": ["5", 1], "seed": seed, "steps": steps, "cfg": 1.0,
                         "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "shotcall"}},
    }
    if ref_image:
        g["40"] = {"class_type": "LoadImage", "inputs": {"image": ref_image}}
        g["41"] = {"class_type": "ResizeImageMaskNode",
                   "inputs": {"input": ["40", 0], "resize_type": "scale total pixels",
                              "resize_type.megapixels": 4, "scale_method": "area"}}
        g["5"]["inputs"]["images.image_1"] = ["41", 0]
    return g


ENTRY = re.compile(r"(?m)^## (IMG-\d+) · (.+)$")


def parse_prompts(path: Path) -> list:
    """从 图片提示词.md 里拆出每个资产项的提示词正文。"""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    heads = list(ENTRY.finditer(text))
    out = []
    for i, m in enumerate(heads):
        body = text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        ref = None
        rm = re.search(r"(?m)^- 参考：(.+)$", body)
        if rm:
            v = rm.group(1).strip()
            if v and v not in ("无", "—", "-"):
                pm = re.search(r"[\u4e00-\u9fa5A-Za-z0-9_/\-]*资产/[^\s、，。；）)]+?\.(?:png|jpg|jpeg|webp)", v)
                ref = pm.group(0).lstrip("/") if pm else None
        pm = re.search(r"(?ms)^### 可复制提示词\s*\n(.*?)(?=\n## |\Z)", body)
        prompt = ""
        if pm:
            lines = [ln[2:] if ln.startswith("> ") else ln.lstrip(">")
                     for ln in pm.group(1).splitlines() if ln.strip().startswith(">")]
            prompt = "\n".join(lines).strip()
        out.append({"id": m.group(1), "name": m.group(2).strip(),
                    "ref": ref, "prompt": prompt})
    return out


def preflight(items: list, project: Path) -> dict:
    """返回 blockers / warnings，供提交前阻断。"""
    blockers, warnings = [], []
    if not items:
        blockers.append("没有选择任何候选")
    for it in items:
        tag = f"{it.get('id','?')} {it.get('name','')}".strip()
        if not (it.get("prompt") or "").strip():
            blockers.append(f"{tag}：缺提示词")
        w, h = it.get("width", DEFAULT_W), it.get("height", DEFAULT_H)
        if w % 32 or h % 32:
            blockers.append(f"{tag}：宽高须为 32 倍数（{w}×{h}）")
        ref = it.get("ref")
        if ref and not (project / ref).exists():
            blockers.append(f"{tag}：参考图不存在 {ref}")
    if not blockers:
        n = sum(1 for it in items if it.get("prompt"))
        warnings.append(f"共 {n} 项待出图，每项 {items[0].get('n', 4)} 张候选")
    return {"blockers": blockers, "warnings": warnings}


def submit(port: int, graph: dict) -> str:
    r = _post(port, "/prompt", {"prompt": graph, "client_id": "shotcall-img"})
    if "prompt_id" not in r:
        raise RuntimeError(f"提交失败: {r}")
    return r["prompt_id"]


def history(port: int, prompt_id: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/history/{prompt_id}", timeout=30) as r:
        return json.loads(r.read().decode())


def download(port: int, item: dict) -> bytes:
    q = urllib.parse.urlencode({"filename": item["filename"],
                                  "subfolder": item.get("subfolder", ""),
                                  "type": item.get("type", "output")})
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/view?{q}", timeout=120) as r:
        return r.read()
