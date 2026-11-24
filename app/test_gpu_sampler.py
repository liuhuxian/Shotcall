#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""显存采样测试（M1-04 / AC-07）。

    python3 app/test_gpu_sampler.py

纯 assert，不依赖 pytest。不写项目文件、不连 8790、不启停 ComfyUI、不提交生成；
真实采样只调用只读的 ``nvidia-smi --query-gpu``。
"""
import inspect
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# ---- 导入 comfywatch：断言 import 不再自动启动后台线程 --------------------
_REAL_THREAD_START = threading.Thread.start
_auto_started = []


def _record_start(self):
    _auto_started.append(self)
    return _REAL_THREAD_START(self)


threading.Thread.start = _record_start
try:
    import comfywatch  # noqa: E402
finally:
    threading.Thread.start = _REAL_THREAD_START

assert not _auto_started, "import comfywatch 不应启动后台线程，实际启动了 %d 个" % len(_auto_started)
assert comfywatch._started is False, "import comfywatch 后 _started 应为 False"
assert callable(getattr(comfywatch, "start", None)), "comfywatch 应有 start()"
assert callable(getattr(comfywatch, "vram_error", None)), "comfywatch 应有 vram_error()"
print("✓ import 无副作用；start()/vram_error() 接口存在")

# ---- start() 幂等：用 FakeThread 记录，不真正起线程/不发网络请求 -----------
_RealThread = threading.Thread


class _FakeThread:
    started = []

    def __init__(self, target=None, daemon=None, **kw):
        self.target = target
        self.daemon = daemon
        _FakeThread.started.append(target)

    def start(self):
        pass


comfywatch._started = False
comfywatch.threading.Thread = _FakeThread
try:
    assert comfywatch.start() is True, "第一次 start() 应返回 True"
    assert comfywatch.start() is False, "第二次 start() 应幂等返回 False"
finally:
    comfywatch.threading.Thread = _RealThread
    comfywatch._started = False

assert len(_FakeThread.started) == 2, "start() 只应启动 2 个后台线程，实际 %d" % len(_FakeThread.started)
assert set(_FakeThread.started) == {comfywatch._poll_queues, comfywatch._sample_vram}, \
    "start() 应启动队列轮询 + 显存采样两个线程"
print("✓ start() 幂等，只启动队列轮询 + 显存采样两个线程")

# ---- 解析函数与源码审计：不碰 torch / mem_get_info / H3_PY -----------------
src = (HERE / "comfywatch.py").read_text(encoding="utf-8")
for banned in ("torch", "mem_get_info", "H3_PY"):
    assert banned not in src, "comfywatch.py 不应再出现 %r" % banned
assert "torch" not in sys.modules, "import comfywatch 不应加载 torch"
assert not hasattr(comfywatch, "H3_PY"), "comfywatch 不应再有 H3_PY"

parse_src = inspect.getsource(comfywatch._parse_vram_csv)
for banned in ("torch", "mem_get_info", "H3_PY", "subprocess", "Popen"):
    assert banned not in parse_src, "解析函数不应引用 %r" % banned
assert comfywatch._parse_vram_csv("") == {}, "空输出应解析为空"
assert comfywatch._parse_vram_csv("garbage, line\nN/A, N/A, N/A, N/A") == {}, "坏行应被跳过"
print("✓ 源码/解析函数无 torch、mem_get_info、H3_PY，不调用子进程")

# ---- mock nvidia-smi：字段换算正确 ----------------------------------------
CSV = "0, 1024, 8192, 5\n1, 4096, 24576, 77\n"


class _Completed:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


_real_run = comfywatch.subprocess.run
_calls = []


def _fake_run(cmd, **kw):
    _calls.append((list(cmd), dict(kw)))
    return _Completed(CSV.encode())


comfywatch.subprocess.run = _fake_run
try:
    with comfywatch._lock:          # 只清理测试进程内的缓存，不碰真实状态
        comfywatch._vram.clear()
        comfywatch._vram_error = None

    assert comfywatch._sample_vram_once() is True, "mock 采样应成功"
    v0, v1 = comfywatch.vram(0), comfywatch.vram(1)
    assert v0["usedGB"] == 1.0, v0
    assert v0["totalGB"] == 8.0, v0
    assert v0["pct"] == 12.5, v0          # 1024/8192
    assert v0["util"] == 5.0, v0          # utilization.gpu
    assert isinstance(v0["ts"], float) and v0["ts"] > 0, v0
    assert v1["usedGB"] == 4.0, v1
    assert v1["totalGB"] == 24.0, v1
    assert v1["pct"] == 16.7, v1          # 4096/24576 = 16.66…
    assert comfywatch.vram(9) == {}, "未采到的卡应返回空字典"
    assert comfywatch.vram_error() is None, comfywatch.vram_error()

    cmd, kw = _calls[-1]
    assert cmd == ["nvidia-smi",
                   "--query-gpu=index,memory.used,memory.total,utilization.gpu",
                   "--format=csv,noheader,nounits"], cmd
    assert kw.get("capture_output") is True, kw
    assert kw.get("timeout") == 2, kw
    assert kw.get("shell") in (None, False), kw
finally:
    comfywatch.subprocess.run = _real_run

print("✓ mock 采样：usedGB/totalGB/pct/util 正确，命令与 timeout=2 正确")

# ---- mock 单次采样 <200ms ------------------------------------------------
comfywatch.subprocess.run = _fake_run
try:
    t0 = time.perf_counter()
    assert comfywatch._sample_vram_once() is True
    mock_ms = (time.perf_counter() - t0) * 1000.0
finally:
    comfywatch.subprocess.run = _real_run

assert mock_ms < 200.0, "单次采样（mock）耗时 %.1fms，应 <200ms" % mock_ms
print("✓ mock 单次采样耗时 %.2f ms（<200ms）" % mock_ms)

# ---- 失败路径：保留旧值、记录错误、不抛异常 -------------------------------
_before0 = dict(comfywatch.vram(0))
_before1 = dict(comfywatch.vram(1))


def _expect_failure(runner, name):
    comfywatch.subprocess.run = runner
    try:
        try:
            ok = comfywatch._sample_vram_once()
        except Exception as e:  # 采样失败绝不能抛异常
            raise AssertionError("%s：采样失败时抛了异常 %r" % (name, e))
        assert ok is False, "%s：应返回 False" % name
        assert comfywatch.vram(0) == _before0, "%s：应保留旧值（卡 0）" % name
        assert comfywatch.vram(1) == _before1, "%s：应保留旧值（卡 1）" % name
        err = comfywatch.vram_error()
        assert err, "%s：应记录 _vram_error" % name
        return err
    finally:
        comfywatch.subprocess.run = _real_run


def _timeout(cmd, **kw):
    raise subprocess.TimeoutExpired(cmd, 2)


def _exit9(cmd, **kw):
    return _Completed(stdout=b"", stderr=b"driver boom", returncode=9)


def _garbage(cmd, **kw):
    return _Completed(stdout=b"N/A, N/A, N/A, N/A\n")


err_timeout = _expect_failure(_timeout, "超时")
err_exit = _expect_failure(_exit9, "退出码非 0")
err_garbage = _expect_failure(_garbage, "输出不可解析")
assert "TimeoutExpired" in err_timeout, err_timeout
assert "9" in err_exit and "RuntimeError" in err_exit, err_exit
assert "无有效行" in err_garbage, err_garbage
print("✓ 失败路径（超时/退出码/坏输出）保留旧值、不抛异常；error=%s" % err_timeout[:60])

# 成功一次后 error 清空
comfywatch.subprocess.run = _fake_run
try:
    assert comfywatch._sample_vram_once() is True
finally:
    comfywatch.subprocess.run = _real_run
assert comfywatch.vram_error() is None, "成功采样后 vram_error 应清空"
print("✓ 采样恢复成功后 _vram_error 清空")

# ---- 真实环境：nvidia-smi 存在则只读采样一次并打印 8 卡 --------------------
smi = shutil.which("nvidia-smi")
if not smi:
    print("- 跳过真实采样：未找到 nvidia-smi")
else:
    with comfywatch._lock:
        comfywatch._vram.clear()
        comfywatch._vram_error = None
    real_ms = 0.0
    ok = False
    for attempt in range(1, 4):        # 卡很忙时 nvidia-smi 偶发 >2s，重试最多 3 次
        t0 = time.perf_counter()
        ok = comfywatch._sample_vram_once()
        real_ms = (time.perf_counter() - t0) * 1000.0
        print("  真实采样第 %d 次：%s，耗时 %.0f ms" %
              (attempt, "成功" if ok else "失败(%s)" % comfywatch.vram_error(), real_ms))
        if ok:
            break
        time.sleep(0.3)
    assert ok, "真实 nvidia-smi 采样失败：%s" % comfywatch.vram_error()
    assert comfywatch.vram_error() is None

    print("  8 卡真实采样结果（nvidia-smi，只读）：")
    seen = 0
    for card in range(8):
        v = comfywatch.vram(card)
        if not v:
            print("    卡%d：无数据" % card)
            continue
        seen += 1
        assert v["totalGB"] > 0, v
        assert 0.0 <= v["pct"] <= 100.0, v
        assert 0.0 <= v.get("util", 0.0) <= 100.0, v
        print("    卡%d：%5.1f / %5.1f GB · 显存 %5.1f%% · 利用率 %3.0f%%" %
              (card, v["usedGB"], v["totalGB"], v["pct"], v.get("util", 0.0)))
    assert seen >= 1, "真实采样应至少解析出一张卡"
    print("✓ 真实采样成功：%d 张卡，耗时 %.0f ms（只读，未启停任何进程）" % (seen, real_ms))

print("\n全部测试通过（纯 assert）")
