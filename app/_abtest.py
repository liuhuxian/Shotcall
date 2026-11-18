# -*- coding: utf-8 -*-
import json, shutil, time
from pathlib import Path
import imagegen, imgjobs

PROJ = Path.home()/"short_v/有谁共鸣2"
EP = "EP001"
PORT = 8195
W, H, STEPS = 2656, 1504, 8

PROM = {
 "IMG-005": """Documentary photograph, 2004 Chinese county-town apartment kitchen. A 43-year-old Chinese man, round face, short black hair, thin metal-frame glasses, faded navy-blue coarse-cloth apron over a rolled-sleeve gray shirt, dark trousers. He stands at a cement counter, both hands holding a cleaver over a wooden board with chopped scallions, torso turned three-quarters to camera, eyes down on the board. Chipped white wall tiles, single-run cement counter, aluminum pot on the stove, warm 25W bulb overhead, weak cold north-window light from the left. Medium-close shot, 50mm lens, eye level. Natural skin with pores, slight sheen on the temple, no beauty filter, film grain, muted warm palette. No text, no logo, no watermark, no stainless-steel range hood, no modern appliances, no bright LED light.""",
 "IMG-008": """Documentary still-life photograph of an empty kitchen, 2004 Chinese county apartment, five to six square meters. One cement counter runs along the wall: chipped enamel sink, two-burner gas stove, wooden cutting board with a cleaver and scattered scallion tops, aluminum pot with the lid ajar, three unwashed enamel bowls in the sink, a glass-lined thermos and a cloth sack of rice on the floor. Faded yellowed white wall tiles with broken glaze, water stains, a grease film near the stove. Early morning: weak cold north-window light from the left, one warm 25W tungsten bulb making a dim yellow pool and a long shadow. No people. Slightly angled medium-wide shot from the doorway, 35mm lens, chest height. Desaturated warm palette, visible dust, film grain, flat contrast. No text, no logo, no watermark, no modern appliances, no microwave, no stainless-steel hood, no bright light."""
}

def run(prompt, out, ref=None):
    g = imagegen.build_graph(prompt, W, H, 42, ref_image=ref, steps=STEPS)
    t0 = time.time(); pid = imagegen.submit(PORT, g)
    while time.time() - t0 < 600:
        h = imagegen.history(PORT, pid)
        if pid in h and h[pid].get("status", {}).get("completed"):
            imgs = [i for v in h[pid].get("outputs", {}).values() for i in v.get("images", [])]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(imagegen.download(PORT, imgs[0]))
            print(f"  ✓ {out.name} {time.time()-t0:.0f}s")
            return True
        time.sleep(3)
    print(f"  ✗ {out.name} 超时")
    return False

ps = {p["id"]: p for p in imagegen.parse_prompts(PROJ/"剧集"/EP/"图片提示词.md")}
for pid in ("IMG-005", "IMG-008"):
    it = ps[pid]
    d = PROJ/"剧集"/EP/"制作成果"/"图片候选"/f"{pid}-{imgjobs._safe_name(it['name'])}"
    d.mkdir(parents=True, exist_ok=True)
    print(f"{pid} {it['name']}")
    if not (d/"A-现行写法.png").exists():
        run(it["prompt"], d/"A-现行写法.png")
    if not (d/"B-模板写法.png").exists():
        run(PROM[pid], d/"B-模板写法.png")
    if pid == "IMG-005" and not (d/"C-模板加参考图.png").exists():
        ref = imgjobs._stage_ref(PORT, PROJ/"资产/定妆照/陈铭5视图.png")
        run(PROM[pid], d/"C-模板加参考图.png", ref=ref)
    else:
        base = next(iter(sorted(d.glob("A-*.png"))), None) if not (d/"C-改图修穿帮.png").exists() else None
        if base:
            ref = imgjobs._stage_ref(PORT, base)
            run(PROM[pid], d/"C-改图修穿帮.png", ref=ref)
print("done")
