# -*- coding: utf-8 -*-
"""H3 Image 出图：提示词解析、图构造、预检。

图结构来自本机实测可用的 ComfyUI 工作流（test_h3_image.py 的 t2i / edit 分支）。
"""
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

UNET = "minimax_h3_image_pruned_bf16.safetensors"
CLIP = "qwen3vl_32b_minimax_h3_bf16.safetensors"
VAE = "minimax_h3_image_vae_fp16.safetensors"

DEFAULT_W, DEFAULT_H = 2656, 1504      # 16:9 · 3.99 MP，均为 32 倍数
DEFAULT_STEPS = 8

# 出图工作流（UI 可选；不影响已知可用的图构造，只控制默认步数/尺寸与标签）
RECIPES = {
    "auto": {"label": "H3 Image · 二合一（有参考图走改图）", "mode": "auto",
             "steps": 6, "width": 2656, "height": 1504,
             "desc": "有参考图走 Edit，无参考图走 T2I"},
    "t2i6": {"label": "H3 Image · 文生图 6 步", "mode": "t2i",
             "steps": 6, "width": 2656, "height": 1504,
             "desc": "纯文生图，6 步"},
    "edit4mp": {"label": "H3 Image · 改图 4MP", "mode": "edit",
                "steps": 6, "width": 2656, "height": 1504,
                "desc": "参考图改图 / image_1 为主图"},
    "t2i8": {"label": "H3 Image · 文生图 8 步", "mode": "t2i",
             "steps": 8, "width": 2656, "height": 1504,
             "desc": "更慢、更细的文生图"},
}
RECIPE_FILE = Path(os.environ.get("SHOTCALL_IMG_RECIPE_FILE")
                   or (Path(__file__).parent / "run" / "image_recipe.json"))


def current_recipe() -> dict:
    name = "auto"
    try:
        name = json.loads(RECIPE_FILE.read_text(encoding="utf-8")).get("name") or "auto"
    except Exception:
        pass
    if name not in RECIPES:
        name = "auto"
    return {"name": name, **RECIPES[name]}


def set_recipe(name: str) -> dict:
    if name not in RECIPES:
        raise ValueError("未知出图工作流")
    RECIPE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RECIPE_FILE.with_name(RECIPE_FILE.name + ".tmp")
    tmp.write_text(json.dumps({"name": name}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(RECIPE_FILE)
    return current_recipe()


def _post(port, path, payload, timeout=60):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def build_graph(prompt: str, width=DEFAULT_W, height=DEFAULT_H, seed=42,
                ref_image: str = None, steps=DEFAULT_STEPS, ref_images: list = None) -> dict:
    """文生图；给了参考图就走 image_1（可多张 image_1..N，第一张为主图）。"""
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
    refs = list(ref_images or ([] if ref_image is None else [ref_image]))
    for i, ref in enumerate(refs[:16]):
        load_id = str(40 + i * 2)
        resize_id = str(41 + i * 2)
        g[load_id] = {"class_type": "LoadImage", "inputs": {"image": ref}}
        g[resize_id] = {"class_type": "ResizeImageMaskNode",
                        "inputs": {"input": [load_id, 0], "resize_type": "scale total pixels",
                                   "resize_type.megapixels": 4, "scale_method": "area"}}
        g["5"]["inputs"][f"images.image_{i + 1}"] = [resize_id, 0]
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
        refs = it.get("refs")
        if refs is None:
            refs = [it.get("ref")] if it.get("ref") else []
        if not isinstance(refs, list):
            blockers.append(f"{tag}：refs 必须是数组")
            refs = []
        for ref in refs[:16]:
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
