#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M2 场次/资产审核回归（AC-11/12/15/16）。"""
import json
import os
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("SHOTCALL_NO_BACKGROUND", "1")

import server

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  ← {detail}"))


def build_project():
    p = Path(tempfile.mkdtemp(prefix="m2-rev-"))
    (p / "05-正式剧本").mkdir(parents=True)
    (p / "07-图像资产").mkdir(parents=True)
    (p / "00-项目管理").mkdir(parents=True)
    script = "# 剧本\n\n|N001／E001-S01|内景|教室|晨|陈铭|动作|\n"
    assets = "# 资产\n\n## 角色\n\n### C01-A｜陈铭\n\n- 描述：x\n"
    (p / "05-正式剧本/screenplay-v001.md").write_text(script, encoding="utf-8")
    (p / "07-图像资产/assets-v001.md").write_text(assets, encoding="utf-8")
    return p, script, assets
def main():
    p, script, assets = build_project()
    (p / "00-项目管理/artifact-state.json").write_text(json.dumps({
        "schema_version": "3.4", "current": {},
        "artifacts": [{"path": "05-正式剧本/screenplay-v001.md", "kind": "05 剧本",
                       "status": "草稿待确认", "sha256": "", "depends_on": [], "scope": "全项目"}]
    }), encoding="utf-8")
    old = server.PROJECT
    server.PROJECT = p
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(path):
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return json.loads(r.read().decode())

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    try:
        sc = get("/api/scenes")
        check("场次带 reviewPath", sc["scenes"][0].get("reviewPath") == "05-正式剧本/screenplay-v001.md", sc["scenes"][0])
        post("/api/approval", {"kind": "scene", "target": "scene:N001", "verdict": "pass",
                               "path": "05-正式剧本/screenplay-v001.md", "scope": {"scene": "N001"}})
        sc2 = get("/api/scenes")
        check("场次审批可见", (sc2["scenes"][0].get("approval") or {}).get("record", {}).get("verdict") == "pass",
              sc2["scenes"][0].get("approval"))
        (p / "05-正式剧本/screenplay-v001.md").write_text(script + "\n", encoding="utf-8")
        sc3 = get("/api/scenes")
        check("05 改动后场次 stale", (sc3["scenes"][0].get("approval") or {}).get("stale") is True,
              sc3["scenes"][0].get("approval"))

        asx = get("/api/assets")
        check("资产带 reviewPath", asx.get("doc") == "07-图像资产/assets-v001.md", asx.get("doc"))
        post("/api/approval", {"kind": "asset", "target": "asset:C01-A", "verdict": "pass",
                               "path": "07-图像资产/assets-v001.md", "scope": {"asset": "C01-A"}})
        asx2 = get("/api/assets")
        check("资产审批可见", (asx2["assets"][0].get("approval") or {}).get("record", {}).get("verdict") == "pass",
              asx2["assets"][0].get("approval"))
        (p / "07-图像资产/assets-v001.md").write_text(assets + "\n", encoding="utf-8")
        asx3 = get("/api/assets")
        check("07 改动后资产 stale", (asx3["assets"][0].get("approval") or {}).get("stale") is True,
              asx3["assets"][0].get("approval"))

        post("/api/artifact/confirm", {"path": "05-正式剧本/screenplay-v001.md", "verdict": "pass",
                                       "scope": "全项目"})
        st = get("/api/stages")
        check("stages 本地状态来自审批", st["artifacts"][0]["local"].get("local") == "confirmed",
              st["artifacts"][0]["local"])
        md = urllib.request.urlopen(base + "/api/reviews/export", timeout=10).read().decode()
        check("导出审核单含场次/资产", "场次" in md and "资产" in md and "scene:N001" in md, md[:200])
    finally:
        srv.shutdown()
        server.PROJECT = old
    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
