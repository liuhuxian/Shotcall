# -*- coding: utf-8 -*-
"""N001 三条串行链：每镜参考上一镜最后 22 帧。

组A/组B/组C 各占一张卡（8192 / 8193 / 8195），种子不同，其余一致。
"""
import json, re, threading, time, urllib.parse, urllib.request
from pathlib import Path

PROJ = Path.home() / "short_v/有谁共鸣2"
MD = PROJ / "09-镜头与视频提示词/视频提示词/N001/minimax-h3-ref2va-v001.md"
OUTDIR = PROJ / "09-镜头与视频提示词/生成结果/N001"

MODELS = dict(
    unet="minimax_h3_ref2va_pruned_bf16.safetensors",
    clip="qwen3vl_32b_minimax_h3_bf16.safetensors",
    vae="minimax_h3_video_vae_fp16.safetensors",
    audio_vae="minimax_h3_audio_vae_fp32.safetensors",
)

UNITS = [
    dict(key="U001", length=311, w=1344, h=768, refs=[
        "07-图像资产/候选图片/f909b45893fd89e4766d9c635cc88f5eeef8a6601725dcf4782afd8b4ada97c2.png",
        "07-图像资产/候选图片/b7fec0a6e0e5c01cc3b2cc82ff5381a33dc892eefbb36392e6772fffeb6904e2.png"]),
    dict(key="U002", length=345, w=1344, h=768, refs=[
        "07-图像资产/候选图片/f909b45893fd89e4766d9c635cc88f5eeef8a6601725dcf4782afd8b4ada97c2.png",
        "07-图像资产/候选图片/1b239c3a362606947e6bcc3ed5e1c2daf1b3614003d587c8208f1fe0b7683811.png",
        "07-图像资产/候选图片/b7fec0a6e0e5c01cc3b2cc82ff5381a33dc892eefbb36392e6772fffeb6904e2.png"]),
    dict(key="U003", length=362, w=1344, h=768, refs=[
        "07-图像资产/候选图片/f909b45893fd89e4766d9c635cc88f5eeef8a6601725dcf4782afd8b4ada97c2.png",
        "07-图像资产/候选图片/83c4513efef0882347528c1946fd148916795157040373b20dfdebb3bad84ab4.png",
        "07-图像资产/候选图片/b7fec0a6e0e5c01cc3b2cc82ff5381a33dc892eefbb36392e6772fffeb6904e2.png"]),
]


def api(port, path, payload=None, timeout=120):
    url = f"http://127.0.0.1:{port}{path}"
    if payload is None:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def upload(port, path: Path, as_name: str = None) -> str:
    fname = as_name or path.name
    boundary = "----chain" + str(int(time.time() * 1000))
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="image"; filename="{fname}"\r\n'.encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n" + path.read_bytes() + b"\r\n"
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/upload/image", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode()).get("name", fname)


def load_prompts():
    txt = MD.read_text(encoding="utf-8")
    blocks = re.findall(r"### (U00\d)｜.*?```text\n(.*?)```", txt, re.S)
    return {k: v.strip() for k, v in blocks}


def build(port, prompt, unit, seed, prev_video=None, ref_names=None, tag="X"):
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODELS["clip"], "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["vae"]}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["audio_vae"]}},
        "5": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "audio_vae": ["4", 0],
            "prompt": prompt, "width": unit["w"], "height": unit["h"],
            "length": unit["length"], "ref_image_size": "match"}},
        "6": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "7": {"class_type": "BasicScheduler", "inputs": {"model": ["1", 0], "scheduler": "simple", "steps": 8, "denoise": 1.0}},
        "8": {"class_type": "BasicGuider", "inputs": {"model": ["1", 0], "conditioning": ["5", 0]}},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["9", 0], "guider": ["8", 0], "sampler": ["6", 0],
            "sigmas": ["7", 0], "latent_image": ["5", 1]}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["10", 0], "vae": ["4", 0]}},
        "13": {"class_type": "CreateVideo", "inputs": {"images": ["11", 0], "audio": ["12", 0], "fps": 24}},
        "14": {"class_type": "SaveVideo", "inputs": {"video": ["13", 0], "filename_prefix": f"chain/{tag}/{unit['key']}",
                                                     "format": "auto", "codec": "auto"}},
    }
    n = 0
    for name in ref_names or []:
        nid = str(40 + n)
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g["5"]["inputs"][f"ref_images.ref_image_{n}"] = [nid, 0]
        n += 1
    if prev_video:
        g["30"] = {"class_type": "LoadVideo", "inputs": {"file": prev_video}}
        g["31"] = {"class_type": "GetVideoComponents", "inputs": {"video": ["30", 0]}}
        g["32"] = {"class_type": "ImageFromBatch", "inputs": {"image": ["31", 0], "batch_index": -22, "length": 22}}
        g["5"]["inputs"]["ref_videos.ref_video_0"] = ["32", 0]
    return g


def run_chain(tag: str, port: int, seed: int, prompts: dict):
    log = []
    def say(msg):
        line = f"[{tag} {time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True); log.append(line)

    outdir = OUTDIR / f"chain-{tag}"
    outdir.mkdir(parents=True, exist_ok=True)
    prev = None
    for unit in UNITS:
        ref_names = [upload(port, PROJ / p, as_name=f"ref_{tag}_{unit['key']}_{i}{Path(p).suffix}")
                     for i, p in enumerate(unit["refs"])]
        g = build(port, prompts[unit["key"]], unit, seed, prev_video=prev, ref_names=ref_names, tag=tag)
        try:
            pid = api(port, "/prompt", {"prompt": g, "client_id": f"chain-{tag}"})["prompt_id"]
        except Exception as e:
            say(f"{unit['key']} 提交失败: {e}")
            return
        say(f"{unit['key']} 已提交 {pid[:8]} refs={len(ref_names)} prev={prev or '无'}")
        t0 = time.time()
        while time.time() - t0 < 3600:
            time.sleep(10)
            try:
                h = api(port, f"/history/{pid}", timeout=30)
            except Exception:
                continue
            if pid in h:
                st = h[pid].get("status", {})
                if not st.get("completed"):
                    say(f"{unit['key']} 失败: {json.dumps(st)[:200]}")
                    return
                items = [i for v in h[pid].get("outputs", {}).values() for i in v.get("images", [])]
                vids = [i for i in items if i.get("filename", "").endswith(".mp4")]
                if not vids:
                    say(f"{unit['key']} 无视频输出"); return
                v = vids[-1]
                q = urllib.parse.urlencode({"filename": v["filename"], "subfolder": v.get("subfolder", ""), "type": "output"})
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/view?{q}", timeout=300) as r:
                    data = r.read()
                dest = outdir / f"{unit['key']}.mp4"
                dest.write_bytes(data)
                meta = v.get("metadata") or {}
                say(f"{unit['key']} 完成 {round(time.time()-t0)}s {meta.get('frame_count')}帧 {dest.name} {len(data)//1024}KB")
                prev = upload(port, dest, as_name=f"prev_{tag}_{unit['key']}.mp4")
                break
        else:
            say(f"{unit['key']} 超时")
            return
    say("链路完成")


JOBS = {"A": (8192, 1001), "B": (8193, 2002), "C": (8188, 3003)}


def main():
    import sys
    which = [a.upper() for a in sys.argv[1:] if a.upper() in JOBS] or list(JOBS)
    prompts = load_prompts()
    if len(which) == 1:
        tag = which[0]
        port, seed = JOBS[tag]
        run_chain(tag, port, seed, prompts)
        return
    threads = [threading.Thread(target=run_chain, args=(t, *JOBS[t], prompts), daemon=False) for t in which]
    for th in threads: th.start()
    for th in threads: th.join()


if __name__ == "__main__":
    main()
