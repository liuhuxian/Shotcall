# -*- coding: utf-8 -*-
"""app/approvals.py 的纯 assert 单测（可直接 python3 app/test_approvals.py）。

约束：
- 只使用 /tmp 下的临时 project；绝不触碰真实项目。
- 不 import server / 不起服务 / 不写任何创作文件。
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import approvals  # noqa: E402


REAL_PROJECT = Path("/users/huxian/short_v/有谁共鸣2")


def _new_project(td: str, name: str = "proj") -> Path:
    p = Path(td) / name
    p.mkdir(parents=True, exist_ok=True)
    resolved = p.resolve()
    # 硬保护：临时项目必须在 /tmp 下，且不能是真实项目。
    assert str(resolved).startswith("/tmp/"), resolved
    assert resolved != REAL_PROJECT and REAL_PROJECT not in resolved.parents
    return resolved


def _ledger(project: Path) -> Path:
    return project / "制作运行" / "approvals.jsonl"


def _valid_lines(project: Path) -> list:
    raw = _ledger(project).read_bytes()
    out = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        out.append(json.loads(line.decode("utf-8")))  # 半行/坏行会在这里炸
    return out


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------- 用例 1


def test_first_append_and_latest() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as td:
        proj = _new_project(td)
        rec = approvals.append(
            proj, "scene", "scene:N001", "pass",
            scope={"unit": "N001"},
            notes=[{"anchor": "第一场", "text": "节奏可以"}],
        )

        assert rec["id"].startswith("APR-"), rec
        assert rec["kind"] == "scene"
        assert rec["target"] == "scene:N001"
        assert rec["verdict"] == "pass"
        assert rec["supersedes"] is None
        assert rec["by"] == "director"
        assert rec["scope"] == {"unit": "N001"}
        assert rec["notes"][0]["anchor"] == "第一场"
        assert rec["path"] is None and rec["sha256"] is None
        assert "T" in rec["at"] and rec["at"][-6] in "+-", rec["at"]

        assert _ledger(proj).is_file()
        lines = _valid_lines(proj)
        assert len(lines) == 1 and lines[0]["id"] == rec["id"]

        live = approvals.latest(proj)
        assert len(live) == 1 and live[0]["id"] == rec["id"]
        st = approvals.state(proj, "scene", "scene:N001")
        assert st["record"]["id"] == rec["id"]
        assert st["stale"] is False
        assert st["shaNow"] is None
        assert st["reason"] == "有效"
        print("  test_first_append_and_latest ok")


# ---------------------------------------------------------------- 用例 2


def test_supersedes_and_latest_only_current() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as td:
        proj = _new_project(td)
        first = approvals.append(proj, "scene", "scene:N001", "pass")
        second = approvals.append(
            proj, "scene", "scene:N001", "revise",
            notes=[{"text": "第二场结尾再压一拍"}],
        )
        assert second["supersedes"] == first["id"]
        assert first["supersedes"] is None

        lines = _valid_lines(proj)
        assert len(lines) == 2, "旧记录必须保留"
        assert [r["id"] for r in lines] == [first["id"], second["id"]]

        live = approvals.latest(proj)
        assert len(live) == 1, live
        assert live[0]["id"] == second["id"]
        assert live[0]["verdict"] == "revise"
        st = approvals.state(proj, "scene", "scene:N001")
        assert st["record"]["id"] == second["id"]
        assert st["stale"] is False

        third = approvals.append(proj, "scene", "scene:N002", "pass")
        assert third["supersedes"] is None, "不同 target 不应串 supersedes"
        live = approvals.latest(proj)
        assert {r["id"] for r in live} == {second["id"], third["id"]}
        assert len(live) == 2

        # read_all 保留全部历史
        assert len(approvals.read_all(proj)) == 3
        print("  test_supersedes_and_latest_only_current ok")


# ---------------------------------------------------------------- 用例 3


def test_file_stale_and_file_ok() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as td:
        proj = _new_project(td)
        doc = proj / "09-镜头与视频提示词" / "N001" / "minimax-h3-ref2va-v009.md"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("第一版提示词\n", encoding="utf-8")
        rel = doc.relative_to(proj).as_posix()

        rec = approvals.append(
            proj, "file", rel, "pass", path=rel, version="v009",
            scope={"unit": "N001"},
        )
        assert rec["path"] == rel, rec
        assert rec["sha256"] == _sha(doc), rec
        assert rec["version"] == "v009"

        st = approvals.state(proj, "file", rel)
        assert st["record"]["id"] == rec["id"]
        assert st["stale"] is False
        assert st["shaNow"] == rec["sha256"]

        got = approvals.file_ok(proj, rel)
        assert got is not None and got["id"] == rec["id"]
        # 绝对路径（项目内）也应能认出来
        assert approvals.file_ok(proj, str(doc))["id"] == rec["id"]

        # 改文件 → stale，file_ok 不再返回
        doc.write_text("第二版提示词（被改过）\n", encoding="utf-8")
        st = approvals.state(proj, "file", rel)
        assert st["stale"] is True, st
        assert st["shaNow"] == _sha(doc)
        assert st["shaNow"] != rec["sha256"]
        assert "改动" in st["reason"], st["reason"]
        assert approvals.file_ok(proj, rel) is None

        # 文件被删 → stale，reason 说明不存在
        doc.unlink()
        st = approvals.state(proj, "file", rel)
        assert st["stale"] is True and st["shaNow"] is None, st
        assert "不存在" in st["reason"], st["reason"]
        assert approvals.file_ok(proj, rel) is None

        # 重新建同路径、重申 pass，则恢复有效
        doc.write_text("第三版提示词\n", encoding="utf-8")
        rec2 = approvals.append(proj, "file", rel, "pass", path=rel,
                                version="v009")
        assert rec2["supersedes"] == rec["id"]
        st = approvals.state(proj, "file", rel)
        assert st["stale"] is False and st["record"]["id"] == rec2["id"], st
        assert approvals.file_ok(proj, rel)["id"] == rec2["id"]

        # 改判为 revise：文件没变也不 stale，但 file_ok 必须 None
        rec3 = approvals.append(proj, "file", rel, "revise", path=rel,
                                version="v009")
        assert rec3["supersedes"] == rec2["id"]
        st = approvals.state(proj, "file", rel)
        assert st["stale"] is False, st
        assert approvals.file_ok(proj, rel) is None

        # 文件审批必须真的有文件
        missing = proj / "09-镜头与视频提示词" / "N001" / "not-there-v001.md"
        try:
            approvals.append(proj, "file", missing.relative_to(proj).as_posix(),
                             "pass")
        except ValueError as exc:
            assert "不存在" in str(exc) or "文件" in str(exc), exc
        else:
            raise AssertionError("缺失文件的 file 审批应当报错")

        # summary 里 stale 计数与 live 计数
        s = approvals.summary(proj)
        assert s["total"] == 3 and s["live"] == 1, s
        assert s["superseded"] == 2, s
        assert s["stale"] == 0, s
        # revise 的文件目前没变，stale 应为 0；再改一下则计入
        doc.write_text("第四版\n", encoding="utf-8")
        s = approvals.summary(proj)
        assert s["stale"] == 1, s
        assert rel in s["stale_targets"], s
        print("  test_file_stale_and_file_ok ok")


# ---------------------------------------------------------------- 用例 4


def test_invalid_kind_and_verdict() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as td:
        proj = _new_project(td)
        bad_kinds = ["artifact", "", "FILE", "files", "unknown", None, 3]
        for bad in bad_kinds:
            try:
                approvals.append(proj, bad, "scene:N001", "pass")
            except ValueError:
                pass
            else:
                raise AssertionError(f"非法 kind 未被拒绝: {bad!r}")

        bad_verdicts = ["confirmed", "reject", "", "PASS", None, 0]
        for bad in bad_verdicts:
            try:
                approvals.append(proj, "scene", "scene:N001", bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"非法 verdict 未被拒绝: {bad!r}")

        assert not _ledger(proj).exists() or _ledger(proj).stat().st_size == 0

        # state 也不能接受非法 kind
        try:
            approvals.state(proj, "artifact", "x")
        except ValueError:
            pass
        else:
            raise AssertionError("state 接受了非法 kind")

        good = approvals.append(proj, "scene", "scene:N001", "pass")
        try:
            approvals.append(proj, "scene", "scene:N001", "bogus")
        except ValueError:
            pass
        else:
            raise AssertionError("非法 verdict 未被拒绝（已有账本时）")
        assert len(_valid_lines(proj)) == 1
        assert approvals.state(proj, "scene", "scene:N001")["record"]["id"] == good["id"]
        print("  test_invalid_kind_and_verdict ok")


# ---------------------------------------------------------------- 用例 5


def test_concurrent_append() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as td:
        proj = _new_project(td)
        n = 20
        barrier = threading.Barrier(n)

        def work(i: int):
            barrier.wait(timeout=20)
            return approvals.append(
                proj, "scene", "scene:CONC", "pass",
                notes=[{"text": f"worker-{i}"}],
            )

        with ThreadPoolExecutor(max_workers=n) as ex:
            results = list(ex.map(work, range(n)))
        assert len(results) == n

        raw = _ledger(proj).read_bytes()
        assert raw.endswith(b"\n"), "账本最后一行必须完整"
        lines = [x for x in raw.splitlines() if x.strip()]
        assert len(lines) == n, f"丢行：{len(lines)} != {n}"
        parsed = [json.loads(x.decode("utf-8")) for x in lines]
        ids = [r["id"] for r in parsed]
        assert len(set(ids)) == n, f"id 重复: {ids}"
        for rec in parsed:
            assert rec["kind"] == "scene" and rec["target"] == "scene:CONC"
            assert rec["verdict"] == "pass"
            assert rec["notes"] and rec["notes"][0]["text"].startswith("worker-")
        # 同一 target 连续 20 条：应形成一条 supersedes 链
        assert sum(1 for r in parsed if r["supersedes"]) == n - 1
        assert parsed[0]["supersedes"] is None

        live = approvals.latest(proj)
        assert len(live) == 1, live
        assert live[0]["id"] == parsed[-1]["id"]
        st = approvals.state(proj, "scene", "scene:CONC")
        assert st["record"]["id"] == parsed[-1]["id"]
        assert approvals.summary(proj)["total"] == n
        assert approvals.summary(proj)["live"] == 1
        print("  test_concurrent_append ok")


# ---------------------------------------------------------------- 用例 6


def test_shot_can_bind_file_sha() -> None:
    """镜级审批可以绑定 09 文件 SHA（FR-B4/C6）；file_ok 只认 file 类。"""
    with tempfile.TemporaryDirectory(dir="/tmp") as td:
        proj = _new_project(td)
        doc = proj / "09-镜头与视频提示词" / "N001" / "shot-plan-v001.md"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("镜头表 v001\n", encoding="utf-8")
        rel = doc.relative_to(proj).as_posix()

        rec = approvals.append(proj, "shot", "shot:N001/SH001", "pass",
                               path=doc, version="v001")  # 传 Path 也应接受
        assert rec["path"] == rel and rec["sha256"] == _sha(doc), rec
        st = approvals.state(proj, "shot", "shot:N001/SH001")
        assert st["stale"] is False and st["shaNow"] == rec["sha256"], st
        assert approvals.file_ok(proj, rel) is None, "shot 审批不能当 file pass 用"

        doc.write_text("镜头表 v001（被改）\n", encoding="utf-8")
        st = approvals.state(proj, "shot", "shot:N001/SH001")
        assert st["stale"] is True and "改动" in st["reason"], st

        # 不带 path 的场/资产审批不参与 SHA 校验
        approvals.append(proj, "asset", "asset:C01-A", "hold")
        st = approvals.state(proj, "asset", "asset:C01-A")
        assert st["stale"] is False and st["shaNow"] is None, st
        print("  test_shot_can_bind_file_sha ok")


# ---------------------------------------------------------------- 用例 7


def test_empty_missing_and_corrupt() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as td:
        # 6a 项目目录都不存在：所有读接口都不崩
        missing = Path(td) / "does-not-exist"
        assert approvals.latest(missing) == []
        assert approvals.read_all(missing) == []
        assert approvals.summary(missing)["total"] == 0
        assert approvals.summary(missing)["live"] == 0
        assert approvals.file_ok(missing, "a/b.md") is None
        st = approvals.state(missing, "scene", "scene:N001")
        assert st["record"] is None and st["stale"] is False, st
        assert st["reason"] == "无审批记录"

        # 6b 空项目（有目录、无账本）
        proj = _new_project(td, "empty")
        assert approvals.latest(proj) == []
        assert approvals.summary(proj)["total"] == 0
        assert approvals.file_ok(proj, "x.md") is None

        # 6c 损坏行：坏 JSON、半行、非法 UTF-8、合法 JSON 但缺字段
        good = approvals.append(proj, "scene", "scene:A", "pass")
        raw = _ledger(proj).read_bytes()
        broken = (
            b'not json at all\n'
            b'{"id": "APR-20260101-9999", "kind": "scene"\n'
            b'\xff\xfe\x00bad bytes\n'
            b'{"kind": "scene", "target": "scene:B", "verdict": "pass"}\n'
            b'{"id": 5, "kind": "scene", "target": "x", "verdict": "pass"}\n'
        )
        with open(_ledger(proj), "ab") as f:
            f.write(broken)
        # 最后半行没有换行，再 append 必须能隔离它而不是拼坏新行
        with open(_ledger(proj), "ab") as f:
            f.write(b'{"id": "APR-20260101-8888", "kind": "scene", "target"')

        live = approvals.latest(proj)  # 不抛异常
        assert [r["id"] for r in live] == [good["id"]], live
        assert approvals.state(proj, "scene", "scene:A")["record"]["id"] == good["id"]
        assert approvals.state(proj, "scene", "scene:B")["record"] is None
        s = approvals.summary(proj)
        assert s["total"] == 1 and s["live"] == 1, s

        # 坏行之后仍能正常追加，且读得回来
        good2 = approvals.append(proj, "scene", "scene:B", "pass")
        raw = _ledger(proj).read_bytes()
        assert raw.endswith(b"\n")
        live = approvals.latest(proj)
        assert {r["id"] for r in live} == {good["id"], good2["id"]}, live
        # 坏行原文仍在（只追加，不删除/覆盖）
        assert b"not json at all" in raw
        assert b"\xff\xfe\x00bad bytes" in raw
        # 全账本可解析记录数：坏行被跳过，只剩 2 条有效审批
        assert len(approvals.read_all(proj)) == 2
        print("  test_empty_missing_and_corrupt ok")


def main() -> int:
    tests = [
        test_first_append_and_latest,
        test_supersedes_and_latest_only_current,
        test_file_stale_and_file_ok,
        test_invalid_kind_and_verdict,
        test_concurrent_append,
        test_shot_can_bind_file_sha,
        test_empty_missing_and_corrupt,
    ]
    for test in tests:
        test()
    print(f"\n通过 {len(tests)} 个测试 · 失败 0")
    print(f"ALL {len(tests)} TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
