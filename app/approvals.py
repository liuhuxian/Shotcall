# -*- coding: utf-8 -*-
"""追加式审批账本（M1-02）。

审批是工作台里唯一的“人做过的判断”的证据。存储约定：

- 文件：``<project>/制作运行/approvals.jsonl``，一行一条 JSON；只追加，
  不覆盖、不删除，改判 = 新记录 + ``supersedes`` 指向旧记录。
- 并发：追加写用 ``fcntl.flock(LOCK_EX)`` + ``os.fsync``；进程内再加一把
  ``threading.Lock``，保证多线程下 id 连续、不会写出半行。
- 读取：共享锁；损坏行（非 JSON、非对象、缺关键字段、非法 UTF-8）一律跳过，
  读取接口不抛异常。
- 文件有效性：记录里带 ``path`` + ``sha256`` 的审批，读状态时重新计算当前
  SHA；文件缺失/被改动 → ``stale=True``。``kind="file"`` 必须带这两个字段，
  其它 kind 若显式传了 ``path``，也会记 SHA（便于镜级审批绑定 09 文件），
  但契约只要求 file 类必须。

层级语义：

- ``latest`` 只返回每个 ``(kind, target)`` 当前有效的那一条（被 supersede 的
  旧记录仍在账本里，但不出现在 latest 中）。
- ``state`` 无记录时返回 ``record=None, stale=False, reason="无审批记录"``；
  调用方必须先判断 ``record``，不能只看 ``stale``。``file_ok`` 已经把
  “无记录 / 非 pass / stale” 三类都收口成 ``None``，门禁应优先用它。
- ``target``：file 用项目内相对路径（也允许绝对路径，但必须在项目内）；
  scene/shot/asset 用稳定 ID，如 ``scene:N001``、``shot:N001/SH001``、
  ``asset:C01-A``。

本模块不写任何创作文件；除了账本本身，只读取文件算 SHA。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

KINDS = ("file", "scene", "asset", "shot", "pick", "batch")
VERDICTS = ("pass", "revise", "hold", "na")

#: 账本相对项目根的路径；Lead 集成/测试可以直接引用。
LEDGER_REL = "制作运行/approvals.jsonl"

_ID_RE = re.compile(r"^APR-(\d{8})-(\d{4,})$")

# 进程内的串行锁；flock 负责跨进程。读取方不拿这把锁（否则会和写方互等）。
_THREAD_LOCK = threading.Lock()


def _root(project) -> Path:
    return Path(project).expanduser().resolve()


def ledger_path(project) -> Path:
    """账本绝对路径（不保证存在）。"""
    return _root(project) / LEDGER_REL


# ---------------------------------------------------------------- 基础工具


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_rel(project, rel):
    """把项目内相对/绝对路径解析成绝对 Path；越界或非法返回 None。"""
    try:
        root = _root(project)
        p = Path(rel).expanduser()
        if not p.is_absolute():
            p = root / p
        resolved = p.resolve()
        resolved.relative_to(root)
        return resolved
    except (OSError, ValueError, TypeError):
        return None


def _normalize_rel(project, raw) -> str:
    """校验并归一化为项目内 POSIX 相对路径；非法时抛 ValueError。"""
    if isinstance(raw, os.PathLike):
        raw = os.fspath(raw)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("必须有项目内文件路径")
    resolved = _resolve_rel(project, raw)
    if resolved is None:
        raise ValueError(f"路径必须在项目内: {raw}")
    try:
        rel = resolved.relative_to(_root(project))
    except ValueError:
        raise ValueError(f"路径必须在项目内: {raw}")
    if not rel.parts:
        raise ValueError("路径必须是项目内文件，不能是项目根目录")
    return rel.as_posix()


def _validate_choice(name: str, value, allowed: tuple) -> None:
    if value not in allowed:
        raise ValueError(f"{name} 必须是 {'/'.join(allowed)} 之一，收到 {value!r}")


def _now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


# ---------------------------------------------------------------- 读取


def _read_raw(project) -> bytes:
    """读账本原始字节；文件不存在/不可读返回 b""，绝不抛异常。"""
    path = ledger_path(project)
    try:
        f = open(path, "rb")
    except OSError:
        return b""
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        except OSError:
            pass
        return f.read()
    except OSError:
        return b""
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def _valid_record(obj) -> bool:
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("id"), str) and bool(obj["id"])
        and obj.get("kind") in KINDS
        and isinstance(obj.get("target"), str) and bool(obj["target"])
        and obj.get("verdict") in VERDICTS
    )


def _parse_records(raw: bytes) -> list:
    out = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            continue  # 半行/坏字节：跳过，不影响其它记录
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if _valid_record(obj):
            out.append(obj)
    return out


def _read_records(project) -> list:
    return _parse_records(_read_raw(project))


def _live_records(records: list) -> list:
    """每个 (kind,target) 取最后一条未被 supersede 的记录，保持账本顺序。"""
    superseded = {
        rec.get("supersedes") for rec in records
        if isinstance(rec.get("supersedes"), str) and rec.get("supersedes")
    }
    positions = {}
    for i, rec in enumerate(records):
        if rec["id"] in superseded:
            continue
        positions[(rec["kind"], rec["target"])] = i
    return [records[i] for i in sorted(positions.values())]


def _pick_live(live: list, kind: str, target: str):
    for rec in reversed(live):
        if rec["kind"] == kind and rec["target"] == target:
            return rec
    return None


def read_all(project) -> list[dict]:
    """账本里所有可解析的记录（含已被替代的旧记录），按写入顺序。"""
    return _read_records(project)


def latest(project) -> list[dict]:
    """每个 (kind,target) 当前有效的一条，按写入顺序。"""
    return _live_records(_read_records(project))


# ---------------------------------------------------------------- 文件有效性


def _hash_state(project, rec) -> tuple:
    """返回 (stale, shaNow, reason)；只对带 path/sha 的记录有意义。"""
    rel = rec.get("path")
    if not isinstance(rel, str) or not rel:
        return True, None, "记录缺少文件路径"
    full = _resolve_rel(project, rel)
    if full is None:
        return True, None, "记录路径非法"
    if not full.is_file():
        return True, None, "文件不存在"
    try:
        sha_now = _sha256_file(full)
    except OSError as exc:
        return True, None, f"文件读取失败: {exc}"
    want = rec.get("sha256")
    if not isinstance(want, str) or not want:
        return True, sha_now, "记录缺少 sha256"
    if sha_now != want:
        return True, sha_now, "文件已改动"
    return False, sha_now, "有效"


def state(project, kind, target) -> dict:
    """返回 ``{record, stale, shaNow, reason}``；无记录时 ``record=None``。

    注意：无记录时 ``stale=False``（没有东西可 stale），调用方必须先看
    ``record``；做门禁请优先用 :func:`file_ok`。
    """
    _validate_choice("kind", kind, KINDS)
    if not isinstance(target, str) or not target.strip():
        raise ValueError("target 必须是非空字符串")

    rec = _pick_live(_live_records(_read_records(project)), kind, target)
    if rec is None:
        return {"record": None, "stale": False, "shaNow": None,
                "reason": "无审批记录"}

    if kind == "file" or rec.get("path"):
        stale, sha_now, reason = _hash_state(project, rec)
    else:
        stale, sha_now, reason = False, None, "有效"
    return {"record": rec, "stale": stale, "shaNow": sha_now, "reason": reason}


def file_ok(project, rel) -> dict | None:
    """只有当前有效的 file ``pass`` 才返回 record，否则 None。

    匹配记录的 ``path``（优先）或 ``target``；路径越界视为不存在。
    """
    if isinstance(rel, os.PathLike):
        rel = os.fspath(rel)
    if not isinstance(rel, str) or not rel.strip():
        return None
    try:
        want = _normalize_rel(project, rel)
    except ValueError:
        return None

    cand = None
    for rec in _live_records(_read_records(project)):
        if rec["kind"] != "file":
            continue
        matched = False
        rec_path = rec.get("path")
        if isinstance(rec_path, str) and rec_path:
            try:
                if _normalize_rel(project, rec_path) == want:
                    matched = True
            except ValueError:
                matched = False
        if not matched and rec.get("target") == rel:
            matched = True
        if not matched:
            target = rec.get("target")
            if isinstance(target, str) and target:
                try:
                    matched = _normalize_rel(project, target) == want
                except ValueError:
                    matched = False
        if matched:
            cand = rec

    if cand is None or cand.get("verdict") != "pass":
        return None
    stale, _sha_now, _reason = _hash_state(project, cand)
    return None if stale else cand


# ---------------------------------------------------------------- 追加


def _next_id(records: list, now: datetime) -> str:
    day = now.strftime("%Y%m%d")
    top = 0
    for rec in records:
        m = _ID_RE.match(rec["id"])
        if m and m.group(1) == day:
            top = max(top, int(m.group(2)))
    return f"APR-{day}-{top + 1:04d}"


def append(project, kind, target, verdict, *, path=None, version=None,
           scope=None, notes=None, by="director") -> dict:
    """追加一条审批并返回记录。

    - ``kind``/``verdict`` 非法、``target`` 为空、文件类缺文件 → ValueError。
    - ``notes`` 传字符串会包成 ``[{"text": ...}]``；``scope`` 必须是对象。
    - 同一 ``kind+target`` 已有当前记录时，新记录自动带 ``supersedes``。
    """
    _validate_choice("kind", kind, KINDS)
    _validate_choice("verdict", verdict, VERDICTS)
    if not isinstance(target, str) or not target.strip():
        raise ValueError("target 必须是非空字符串")
    if not isinstance(by, str) or not by.strip():
        raise ValueError("by 必须是非空字符串")
    if version is not None and not isinstance(version, str):
        raise ValueError("version 必须是字符串或 None")
    if scope is None:
        scope = {}
    if not isinstance(scope, dict):
        raise ValueError("scope 必须是对象")
    if notes is None:
        notes = []
    elif isinstance(notes, str):
        notes = [{"text": notes}]
    elif isinstance(notes, (list, tuple)):
        notes = list(notes)
    else:
        raise ValueError("notes 必须是数组或字符串")

    try:
        json.dumps({"scope": scope, "notes": notes}, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"scope/notes 无法序列化为 JSON: {exc}")

    root = _root(project)
    if not root.is_dir():
        raise ValueError(f"项目目录不存在: {root}")

    path_val = None
    sha_val = None
    if kind == "file" or path is not None:
        raw_path = path if path is not None else target
        rel = _normalize_rel(root, raw_path)
        full = root / rel
        if not full.is_file():
            raise ValueError(f"文件不存在: {rel}")
        path_val = rel
        sha_val = _sha256_file(full)

    book = ledger_path(root)
    now = _now()

    with _THREAD_LOCK:
        book.parent.mkdir(parents=True, exist_ok=True)
        with open(book, "a+b") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read()
                records = _parse_records(raw)
                prev = _pick_live(_live_records(records), kind, target)
                rec = {
                    "id": _next_id(records, now),
                    "kind": kind,
                    "target": target,
                    "path": path_val,
                    "sha256": sha_val,
                    "version": version,
                    "verdict": verdict,
                    "scope": scope,
                    "notes": notes,
                    "supersedes": prev["id"] if prev else None,
                    "by": by,
                    "at": now.isoformat(timespec="seconds"),
                }
                line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
                f.seek(0, os.SEEK_END)
                # 若上一次写入在崩溃时留下无换行的半行，先补一个换行把它隔离，
                # 而不是覆盖/删除它（账本只追加）。
                if raw and not raw.endswith(b"\n"):
                    f.write(b"\n")
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    return rec


# ---------------------------------------------------------------- 汇总


def summary(project) -> dict:
    """账本概览：总数、当前有效数、按 kind/verdict 计数、stale 文件数。"""
    records = _read_records(project)
    live = _live_records(records)

    by_kind = {k: 0 for k in KINDS}
    by_verdict = {v: 0 for v in VERDICTS}
    stale_targets = []
    for rec in live:
        by_kind[rec["kind"]] = by_kind.get(rec["kind"], 0) + 1
        by_verdict[rec["verdict"]] = by_verdict.get(rec["verdict"], 0) + 1
        if rec["kind"] == "file" or rec.get("path"):
            stale, _sha_now, _reason = _hash_state(project, rec)
            if stale:
                stale_targets.append(rec["target"])

    return {
        "total": len(records),
        "live": len(live),
        "superseded": len(records) - len(live),
        "by_kind": by_kind,
        "by_verdict": by_verdict,
        "stale": len(stale_targets),
        "stale_targets": stale_targets,
        "records": live,
        "path": str(ledger_path(project)),
    }
