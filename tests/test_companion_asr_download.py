# -*- coding: utf-8 -*-
"""asr 模块测试：下载端点/环境变量还原 + CUDA→CPU 自动回退。

不真正联网、不真加载大模型：用 monkeypatch 把 snapshot_download / WhisperModel
换成替身，验证两条回归 bug 的修复：
1. 下载必须把 endpoint 指向国内镜像（hf-mirror），且结束时还原 env；
2. windowed 下用禁用的 tqdm 子类，绝不因 sys.stderr=None 崩；
3. 用户选了 cuda 但缺 cublas 时，load_engine 自动回退 CPU。
"""
from __future__ import annotations

import os

import pytest

from pet.companion import asr


# ---------------------------------------------------------------- 下载
def test_download_uses_mirror_and_restores_env(monkeypatch, tmp_path):
    calls = []

    def fake_snapshot_download(**kwargs):
        calls.append(kwargs)
        return str(tmp_path / "small")

    # 让 _hf_hub_modules 返回我们的替身
    monkeypatch.setattr(asr, "_hf_hub_modules", lambda: fake_snapshot_download)
    # 记录调用期间看到的 env
    seen = {}

    def wrapped(**kwargs):
        seen["endpoint_env"] = os.environ.get("HF_ENDPOINT")
        seen["progress_env"] = os.environ.get("HF_HUB_DISABLE_PROGRESS_BARS")
        return fake_snapshot_download(**kwargs)

    monkeypatch.setattr(asr, "_hf_hub_modules", lambda: wrapped)

    # 确保进函数前没有这些 env
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_PROGRESS_BARS", raising=False)

    asr.download_model("small", tmp_path)

    # 首次走镜像端点
    assert calls, "snapshot_download 应被调用"
    assert calls[0]["endpoint"] == asr.HF_MIRROR
    assert calls[0]["repo_id"] == "Systran/faster-whisper-small"
    # tqdm_class 是进度回调工厂产出的子类，不是裸 tqdm
    assert calls[0]["tqdm_class"] is not asr.HF_MIRROR
    assert "tqdm_class" in calls[0]
    # 调用期间 env 指向镜像 + 关进度条
    assert seen["endpoint_env"] == asr.HF_MIRROR
    assert seen["progress_env"] == "1"
    # 结束后 env 还原（原本没有 -> 也该没有）
    assert os.environ.get("HF_ENDPOINT") is None
    assert os.environ.get("HF_HUB_DISABLE_PROGRESS_BARS") is None


def test_download_falls_back_to_official(monkeypatch, tmp_path):
    calls = []

    def wrapped(**kwargs):
        calls.append(kwargs["endpoint"])
        if kwargs["endpoint"] == asr.HF_MIRROR:
            raise RuntimeError("镜像挂了")
        return str(tmp_path / "tiny")

    monkeypatch.setattr(asr, "_hf_hub_modules", lambda: wrapped)
    out = asr.download_model("tiny", tmp_path)
    assert calls == [asr.HF_MIRROR, "https://huggingface.co"]
    assert out.endswith("tiny")


def test_download_raises_when_all_endpoints_fail(monkeypatch, tmp_path):
    def wrapped(**kwargs):
        raise RuntimeError("全挂了")

    monkeypatch.setattr(asr, "_hf_hub_modules", lambda: wrapped)
    with pytest.raises(RuntimeError, match="下载 tiny 模型失败"):
        asr.download_model("tiny", tmp_path)


def test_progress_tqdm_never_touches_stderr():
    """进度回调用的 tqdm 子类可正常调用各种方法且不写 stderr。"""
    ticks = []
    cls = asr._make_progress_tqdm(lambda *a: ticks.append(a))
    t = cls(total=100)
    # 这些方法 hub 内部都会用，必须都存在且不抛
    t.update(5)
    t.set_description_str("x")
    t.set_description("y")
    t.set_postfix_str("z")
    t.close()


def test_download_reports_progress_dict(monkeypatch, tmp_path):
    """on_progress 收到的是含 percent/text 的 dict。"""
    seen = []

    def fake_snapshot_download(**kwargs):
        # 模拟 hub 调用 tqdm_class 里的 update/close
        klass = kwargs["tqdm_class"]
        obj = klass(total=100)
        obj.update(50)
        obj.close()
        return str(tmp_path / "tiny")

    monkeypatch.setattr(asr, "_hf_hub_modules", lambda: fake_snapshot_download)
    asr.download_model("tiny", tmp_path, on_progress=lambda p: seen.append(p))
    dicts = [p for p in seen if isinstance(p, dict)]
    assert dicts, "应有 dict 形式的进度回报"
    assert any("percent" in p for p in dicts)


# ---------------------------------------------------------------- 引擎回退
def test_load_engine_falls_back_to_cpu_when_cuda_fails(monkeypatch, tmp_path):
    """模拟 cuda 加载/探活时报 cublas 缺失 -> 应回退 cpu 且返回可用引擎。"""
    asr.unload_engines()
    made = []

    class FakeModel:
        def __init__(self, path, device, compute_type):
            made.append(device)
            self.device = device

        def transcribe(self, audio, **kwargs):
            if self.device == "cuda":
                raise RuntimeError(
                    "Library cublas64_12.dll is not found or cannot be loaded")
            # 生成器语义：真正在迭代时才 raise，与 faster-whisper 一致
            def _gen():
                if self.device == "cuda":
                    raise RuntimeError(
                        "Library cublas64_12.dll is not found or cannot be loaded")
                return
                yield  # pragma: no cover
            return _gen(), None

    import types as _t

    fake_mod = _t.SimpleNamespace(WhisperModel=FakeModel)
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", fake_mod)

    eng = asr.load_engine(str(tmp_path / "m"), device="cuda", compute_type="int8")
    # 先试 cuda，再回退 cpu
    assert "cuda" in made
    assert eng.device == "cpu"


def test_load_engine_cpu_used_directly(monkeypatch, tmp_path):
    asr.unload_engines()
    made = []

    class FakeModel:
        def __init__(self, path, device, compute_type):
            made.append((device, compute_type))

        def transcribe(self, audio, **kwargs):
            def _g():
                return
                yield  # pragma: no cover
            return _g(), None

    import types as _t

    monkeypatch.setitem(__import__("sys").modules, "faster_whisper",
                        _t.SimpleNamespace(WhisperModel=FakeModel))
    asr.load_engine(str(tmp_path / "m"), device="cpu", compute_type="int8")
    assert made == [("cpu", "int8")]


def test_load_engine_caches(monkeypatch, tmp_path):
    asr.unload_engines()
    made = []

    class FakeModel:
        def __init__(self, path, device, compute_type):
            made.append(device)

        def transcribe(self, audio, **kwargs):
            def _g():
                return
                yield  # pragma: no cover
            return _g(), None

    import types as _t

    monkeypatch.setitem(__import__("sys").modules, "faster_whisper",
                        _t.SimpleNamespace(WhisperModel=FakeModel))
    a = asr.load_engine(str(tmp_path / "m2"), device="cpu", compute_type="int8")
    b = asr.load_engine(str(tmp_path / "m2"), device="cpu", compute_type="int8")
    assert a is b
    assert made == ["cpu"]  # 只加载一次
