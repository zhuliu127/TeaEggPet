# -*- coding: utf-8 -*-
"""语音助手（推挽式 / push-to-talk）：按住语音键说话 → 松开用 Whisper 识别 → 进对话。

工作方式（2026-11 改成推挽式，彻底删掉唤醒词/结束词/常驻监听）
----------------------------------------------------------------
1. **装一个全局键盘钩子**（``ptt_hotkey.PushToTalkHotkey``）监听「语音键」
   （默认 ``Win+Alt``，可在设置里改）。平时**完全不碰麦克风、不开常驻流**。
2. **按住**语音键 → 打开麦克风开始录音（``pressed`` 信号）。
3. **松开**语音键 → 停止录音，把这段音频交给本地 Whisper 转成文字
   （``released`` 信号）。
4. 文字非空 → 当作打字一样发进 AI 对话：程序自动调用 LLM 生成回复，
   并用本地 TTS 把回复念出来（复用既有聊天窗的朗读通道），同时在桌宠冒气泡。
5. 语音里的「自拍 / 溜达 / 回家」类指令也照常识别（说「自拍」先二次确认）。

设计要点
--------
* **绝不在 GUI 主线程加载模型**：模型加载放到后台线程，``start()`` 立刻返回，
  不会卡住设置对话框/程序（历史上这里卡死过多次）。
* **录音/识别都在后台线程**，对 UI 的通知走 Qt 信号（queued connection 回 GUI 线程）。
* 发声（``_say``/``_do_speak``）用 ``speak_requested`` 信号投递回 GUI 线程执行——
  ``QMediaPlayer`` 只能在主线程用，后台线程直接放会静音/搞坏事件循环。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Optional

from PySide6.QtCore import QObject, Qt, Signal, QTimer

from .ptt_hotkey import PushToTalkHotkey, is_supported as _ptt_supported

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK_MS = 30
BLOCK = int(SAMPLE_RATE * BLOCK_MS / 1000)

# 语音里可识别的指令关键词（同义词组）。唤醒/结束词已删除，这里只留动作指令。
SELFIE_WORDS = ["自拍", "拍一张", "拍个照", "拍照", "来张照片", "来个自拍", "拍张照"]
WALK_WORDS = ["溜达", "散步", "出去走走", "出门走走", "逛街", "出去逛", "走走"]
HOME_WORDS = ["回家", "回来", "回来吧", "该回家了", "回窝"]
CONFIRM_WORDS = ["确认", "确定", "好的", "好", "是", "可以", "行", "嗯", "对", "拍吧", "来"]
CANCEL_WORDS = ["不用", "算了", "取消", "不要", "别", "不了", "否", "不是"]

# 单段录音上限：按住超过这个时长自动停止并识别（防止忘记松手一直占着麦克风）。
MAX_RECORD_SEC = 30.0

# 简体中文引导 prompt：faster-whisper 的中文模型训练数据偏繁体，不加引导常输出繁体
# （用户看到「電池的社會」「終於成功了」即为繁体误识），引导后能稳定输出简体。
ZH_SIMPLIFIED_PROMPT = "以下是普通话的句子，请用简体中文输出。"

# 高通滤波截止频率：去掉 80Hz 以下的低频嗡声/电流声（空调、风扇、桌面共振多在 60Hz 附近），
# 能明显提升低信噪比录音的可懂度，还顺带减少繁体输出。
HIGHPASS_FC = 80.0

_PUNCT = re.compile(r"[\s，。！？、,.!?\-—…~·\"'“”‘’《》()（）\[\]【】:：;；]+")


def _biquad_highpass(x, sr: int, fc: float):
    """自包含二阶 Butterworth 高通（RBJ biquad），无需 scipy。"""
    import numpy as np

    a = np.asarray(x, dtype=np.float32).reshape(-1)
    if a.shape[0] == 0:
        return a
    w0 = 2.0 * np.pi * float(fc) / float(sr)
    cosw0 = float(np.cos(w0))
    sinw0 = float(np.sin(w0))
    # Q = 1/sqrt(2)（Butterworth）
    alpha = sinw0 / (2.0 * (1.0 / np.sqrt(2.0)))
    b0 = (1.0 + cosw0) / 2.0
    b1 = -(1.0 + cosw0)
    b2 = (1.0 + cosw0) / 2.0
    a0 = 1.0 + alpha
    a1 = -2.0 * cosw0
    a2 = 1.0 - alpha
    b0 /= a0; b1 /= a0; b2 /= a0; a1 /= a0; a2 /= a0
    y = np.empty_like(a)
    x1 = x2 = y1 = y2 = 0.0
    for i in range(a.shape[0]):
        v = float(a[i])
        out = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2 = x1; x1 = v; y2 = y1; y1 = out; y[i] = out
    return y.astype(np.float32)


def _highpass(x, sr: int = SAMPLE_RATE, fc: float = HIGHPASS_FC):
    """二阶 butterworth 高通，去低频噪声（自包含实现，不依赖 scipy）。"""
    try:
        return _biquad_highpass(x, sr, fc)
    except Exception:  # noqa: BLE001
        import numpy as np

        return np.asarray(x, dtype=np.float32).reshape(-1)


def _norm(text: str) -> str:
    return _PUNCT.sub("", str(text or "")).lower()


def _contains_any(text: str, words: list[str]) -> str:
    norm = _norm(text)
    for w in words:
        if _norm(w) and _norm(w) in norm:
            return w
    return ""


# ASR 幻听/字幕套话过滤词表（ASR 听到背景音/静音时会输出字幕套话，命中即丢弃）。
_HALLUCINATION_PHRASES = (
    "谢谢观看", "感谢观看", "请订阅", "订阅", "点赞", "字幕", "出品", "独家",
    "禁止转载", "未经允许", "网易云音乐", "qq音乐", "伴奏", "纯音乐",
    "bilibili", "哔哩哔哩", "字幕由", "明镜与点点", "未经许可",
    "索兰娅", "zither", "harper", "harps", "subtitle", "subtitles",
    "amara", "音译", "校对", "自动生成", "製作人", "制作人",
    # v1.4.33：initial_prompt 在静音/低信噪比段会被 whisper 复述出来，必须过滤，
    # 否则「请用简体中文输出。」会被当成用户说的话送进 AI 对话。
    "请用简体", "简体中文", "以下是普通话", "普通话的句子", "用简体",
)


def _looks_like_subtitle_junk(text: str) -> bool:
    t = _norm(text)
    if not t:
        return True
    for phrase in _HALLUCINATION_PHRASES:
        if _norm(phrase) and _norm(phrase) in t:
            return True
    return False


def _is_prompt_echo(text: str) -> bool:
    """检测 whisper 把 initial_prompt 复述出来的情况（静音/低信噪比时常发生）。

    只要识别结果里出现提示词的显著片段（如「请用简体」「以下是普通话」「用简体中文输出」），
    就认为它是提示词回声、不是用户真的说的话，必须丢弃。
    """
    t = _norm(text)
    if not t:
        return False
    for frag in ("请用简体", "简体中文输出", "以下是普通话", "普通话的句子", "用简体中文", "简体中文"):
        if _norm(frag) in t:
            return True
    return False


def _looks_hallucinated(text: str) -> bool:
    """整段几乎全是同一小片段重复 → 幻听（如「小鱼小鱼小鱼…」）。"""
    t = _norm(text)
    if not t:
        return True
    n = len(t)
    if n < 4:
        return False
    # 短文本：整句就是同一片段重复 ≥2 次（如「三星火三星火」「小鱼小鱼」），几乎必是低质量
    # 音频上的幻听/复读——直接判幻听，别送进对话。
    for seg_len in (2, 3, 4):
        if n == seg_len * 2 and n >= 4:
            half = t[:seg_len]
            if half and t[seg_len:] == half:
                return True
    if n < 8:
        return False
    for seg_len in (2, 3, 4):
        if n < seg_len * 4:
            continue
        counts: dict[str, int] = {}
        for i in range(n - seg_len + 1):
            seg = t[i:i + seg_len]
            counts[seg] = counts.get(seg, 0) + 1
        if counts and max(counts.values()) >= max(4, n // seg_len - 1):
            return True
    return False


def _zero_crossing_rate(x) -> float:
    """过零率（参考 dsh-dafeiyu-pro §8.13，是有声/噪声最有效的判据之一）。"""
    import numpy as np

    a = np.asarray(x, dtype=np.float32).reshape(-1)
    if a.shape[0] < 2:
        return 0.0
    s = np.signbit(a)
    return float(np.mean(s[1:] != s[:-1]))


def _voiced_ratio(x, frame: int = 480) -> float:
    """有声帧占比：能量超过峰值一定比例的帧数占总帧数（断续/噪声偏低）。"""
    import numpy as np

    a = np.asarray(x, dtype=np.float32).reshape(-1)
    if a.shape[0] < frame:
        return 0.0
    n = a.shape[0] // frame
    frames = a[: n * frame].reshape(n, frame)
    energies = np.sqrt(np.mean(frames * frames, axis=1))
    peak = float(energies.max())
    if peak <= 1e-6:
        return 0.0
    return float(np.mean(energies > peak * 0.25))


def _fir_lowpass(x, cutoff_hz: float, sr: int, taps: int = 63):
    """自包含窗函数法 FIR 低通（Hamming 窗），用于降采样前抗混叠。"""
    import numpy as np

    a = np.asarray(x, dtype=np.float32).reshape(-1)
    if a.shape[0] == 0:
        return a
    fc = max(1e-4, min(0.499, float(cutoff_hz) / float(sr)))
    n = np.arange(taps, dtype=np.float64) - (taps - 1) / 2.0
    h = 2.0 * fc * np.sinc(2.0 * fc * n)
    h *= np.hamming(taps)
    h /= np.sum(h) + 1e-12
    # 'same' 卷积
    y = np.convolve(a.astype(np.float64), h, mode="same")
    return y.astype(np.float32)


def _resample_to_16k(x, src_sr: int):
    """把任意采样率的单声道 float32 数组重采样到 16kHz。

    整数倍降采样（如 48k→16k = 3:1）先 FIR 低通抗混叠再抽取，避免线性插值的
    高频混叠拉低 ASR 准确率；非整数倍退回线性插值。
    """
    import numpy as np

    a = np.asarray(x, dtype=np.float32).reshape(-1)
    if src_sr == SAMPLE_RATE or a.shape[0] == 0:
        return a
    ratio = float(src_sr) / float(SAMPLE_RATE)
    if abs(ratio - round(ratio)) < 1e-6 and round(ratio) >= 2:
        # 抗混叠低通到目标奈奎斯特（8kHz）附近，再等间隔抽取
        lp = _fir_lowpass(a, SAMPLE_RATE * 0.5 * 0.9, src_sr)
        step = int(round(ratio))
        return np.ascontiguousarray(lp[::step]).astype(np.float32)
    need = int(round(a.shape[0] * SAMPLE_RATE / src_sr))
    if need <= 0:
        return np.zeros(0, dtype=np.float32)
    src = np.linspace(0.0, 1.0, a.shape[0], endpoint=False, dtype=np.float32)
    dst = np.linspace(0.0, 1.0, need, endpoint=False, dtype=np.float32)
    return np.interp(dst, src, a).astype(np.float32)


def list_input_devices() -> list:
    """返回所有输入设备 [(序号, 名称, 声道数), ...]（供设置页/测试用）。"""
    try:
        import sounddevice as sd
    except Exception:  # noqa: BLE001
        return []
    out = []
    try:
        for idx, dev in enumerate(sd.query_devices()):
            ch = int(dev.get("max_input_channels", 0))
            if ch > 0:
                out.append((idx, str(dev.get("name", "")), ch))
    except Exception:  # noqa: BLE001
        return []
    return out


def probe_mic(device_name: str = "", seconds: float = 3.0, on_level=None) -> dict:
    """测麦克风能否正常收音：录 seconds 秒，边录边回调 on_level(rms)，返回统计。

    同名设备（Windows 常把同一支麦在 MME/DirectSound/WASAPI/WDM-KS 各登记一份）
    按「WASAPI 优先、MME 靠后、声道多优先」排序，逐个用**设备原生采样率**开流试录，
    取平均电平最大的那个。返回 {ok, rms, peak, sr, device, error}。
    """
    import numpy as np

    result = {"ok": False, "rms": 0.0, "peak": 0.0, "sr": 0, "device": None, "error": ""}
    try:
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"没有 sounddevice：{exc}"
        return result
    try:
        hostapis = sd.query_hostapis()
    except Exception:  # noqa: BLE001
        hostapis = []
    devices = sd.query_devices()

    # 收集候选：同名输入设备按 WASAPI 优先/MME 靠后排序
    name = (device_name or "").strip()
    cands = []
    for idx, dev in enumerate(devices):
        ch = int(dev.get("max_input_channels", 0))
        if ch <= 0:
            continue
        if name and name not in str(dev.get("name", "")):
            continue
        try:
            api = str(hostapis[dev.get("hostapi", -1)].get("name", ""))
        except Exception:  # noqa: BLE001
            api = ""
        cands.append((idx, api, ch))
    if not cands:
        # 没指定名字或没匹配到 → 用系统默认输入设备
        try:
            dflt = sd.query_devices(kind="input")
            cands = [(None, "", int(dflt.get("max_input_channels", 1)))]
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"找不到可用输入设备：{exc}"
            return result
    cands.sort(key=lambda t: (1 if "WASAPI" in t[1].upper() else 0,
                              0 if "MME" in t[1].upper() else 1,
                              t[2]), reverse=True)

    best = None  # (mean_rms, peak, sr, idx)
    last_err = ""
    for idx, _api, _ch in cands:
        try:
            info = sd.query_devices(idx) if idx is not None else sd.query_devices(kind="input")
            sr = int(info.get("default_samplerate") or 44100)
            block = max(1, int(sr * 0.03))
            frames = int(seconds * sr)
            stream = sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                                    blocksize=block, device=idx)
            stream.start()
            collected = []
            left = frames
            try:
                while left > 0:
                    data, _of = stream.read(min(block, left))
                    raw = data[:, 0] if data.ndim > 1 else data
                    chunk = np.asarray(raw, dtype=np.float32).copy()
                    collected.append(chunk)
                    left -= chunk.shape[0]
                    if chunk.size:
                        r = float(np.sqrt(np.mean(chunk ** 2)) + 1e-12)
                        if on_level is not None:
                            try:
                                on_level(r)
                            except Exception:  # noqa: BLE001
                                pass
            finally:
                try:
                    stream.stop(); stream.close()
                except Exception:  # noqa: BLE001
                    pass
            audio = np.concatenate(collected) if collected else np.zeros(0, dtype=np.float32)
            if audio.size == 0:
                last_err = "没读到音频数据"
                continue
            mean_rms = float(np.sqrt(np.mean(audio ** 2)) + 1e-12)
            peak = float(np.max(np.abs(audio)))
            if best is None or mean_rms > best[0]:
                best = (mean_rms, peak, sr, idx)
            # 已经足够响，不用再试别的端点
            if mean_rms > 0.01:
                break
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            continue

    if best is None:
        result["error"] = last_err or "所有麦克风端点都打不开"
        return result
    mean_rms, peak, sr, idx = best
    result.update({
        "rms": mean_rms,
        "peak": peak,
        "sr": sr,
        "device": idx,
        "ok": bool(mean_rms > 0.001 or peak > 0.02),
    })
    if not result["ok"]:
        result["error"] = "麦克风几乎没电平（可能静音/选错设备/硬件没在收音）"
    else:
        # 电平提示：识别准确率很依赖录音电平；peak 太低说明说话太小/离麦太远，
        # 在低信噪比下 whisper 容易把话听成乱码（如「三星火」）。
        if peak < 0.05:
            result["level_hint"] = "电平太低：请靠近麦克风或调大系统麦克风音量后重试"
            result["level"] = "low"
        elif peak < 0.15:
            result["level_hint"] = "电平偏小：靠近一点说话识别更准"
            result["level"] = "mid"
        else:
            result["level_hint"] = "电平正常"
            result["level"] = "good"
    return result


class VoiceAssistantError(RuntimeError):
    pass


class VoiceAssistant(QObject):
    """推挽式语音助手（单例由 runtime 持有）。"""

    status = Signal(str)                 # 状态栏/气泡一句话
    state_changed = Signal(str)          # "idle" | "recording" | "processing" | "confirm" | "off"
    heard = Signal(str)                  # 识别到的整句（调试/显示）
    error = Signal(str)
    # 跨线程发声请求：后台线程 emit，GUI 线程槽执行（QMediaPlayer 只能主线程用）。
    speak_requested = Signal(str)
    # 跨线程派发请求：识别在后台线程完成，必须经信号投递回 GUI 线程执行
    #（后台线程里 QTimer.singleShot 没有事件循环不会触发——这是「识别成功却没进对话」的根因）。
    dispatch_requested = Signal(str)

    def __init__(self, app, parent=None) -> None:
        super().__init__(parent)
        self._app = app
        self._engine = None
        self._state = "off"
        self._pending_selfie = False   # 自拍二次确认中
        self._hotkey: Optional[PushToTalkHotkey] = None
        self._model_main_info = ""
        # 录音相关
        self._rec_thread: Optional[threading.Thread] = None
        self._rec_stop = threading.Event()
        self._rec_stream = None
        self._rec_chunks: list = []
        self._rec_dev_index: int | None = None
        # 上次识别成功时的设备索引：下次优先用它（避开死端点）。None=还没确定。
        self._last_good_device: int | None = None
        # 静音窗口：她自己说话时段的回声要挡掉（仅本地 TTS 我们自己设的窗口）。
        self._mute_until = 0.0
        try:
            self.speak_requested.connect(self._do_speak)
        except Exception:  # noqa: BLE001
            pass
        try:
            # QueuedConnection：后台识别线程 emit，GUI 线程跑 _dispatch（含开聊天窗/发消息）。
            self.dispatch_requested.connect(self._dispatch, Qt.QueuedConnection)
        except Exception:  # noqa: BLE001
            try:
                self.dispatch_requested.connect(self._dispatch)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 配置
    def _store(self):
        from . import runtime

        return runtime.companion_store()

    def _cfg(self, key: str, default: Any = None) -> Any:
        store = self._store()
        if store is None:
            return default
        try:
            return store.get(key, default)
        except Exception:  # noqa: BLE001
            return default

    def _enabled(self) -> bool:
        return bool(self._cfg("companion_voice_enabled", False))

    def hotkey_combo(self) -> str:
        return str(self._cfg("companion_voice_hotkey", "win+alt") or "win+alt")

    @property
    def state(self) -> str:
        return self._state

    # ------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        """装全局语音键钩子（幂等，非阻塞）。返回是否已就绪。"""
        if self._hotkey is not None and self._hotkey.installed:
            return True
        if not self._enabled():
            self._set_state("off")
            return False
        if not _ptt_supported():
            self.error.emit("当前系统不支持全局语音键（仅 Windows 可用）。")
            self._set_state("off")
            return False
        hk = PushToTalkHotkey(self.hotkey_combo(), self)
        hk.pressed.connect(self._on_pressed)
        hk.released.connect(self._on_released)
        ok = hk.install()
        if not ok:
            self.error.emit("语音键监听安装失败（可能被安全软件拦截）。可在设置里换一个组合键或重试。")
            self._set_state("off")
            return False
        self._hotkey = hk
        self._set_state("idle")
        self.status.emit("语音助手已就绪：按住「%s」说话，松开我就识别并回复～" % self.hotkey_combo())
        # 后台预加载识别模型：否则第一次按住说话时模型还在加载，音频会被丢光。
        self._warmup_model()
        # 后台预探测麦克风端点：把「按住→能录音」的延迟从首次按键时的多次试录里省掉。
        self._warmup_device()
        return True

    def _warmup_device(self) -> None:
        """后台线程预解析可用麦克风端点并缓存到 self._usable_device。

        首次按键若现场逐个试录多个端点（每个 0.3s），会明显拖慢「按住→开始录音」。
        在启动时先探一次，之后按键直接用缓存端点开流，按住即可录音。
        """
        if getattr(self, "_usable_device", None) is not None:
            return

        def _go():
            try:
                dev = self._pick_device()
                if dev is not None:
                    log.info("麦克风端点预探测完成：device=%s", dev)
            except Exception:  # noqa: BLE001
                log.debug("麦克风端点预探测失败", exc_info=True)

        threading.Thread(target=_go, name="VoiceWarmupDevice", daemon=True).start()

    def stop(self) -> None:
        """摘掉钩子、停止录音、关流（幂等，不阻塞）。"""
        self._rec_stop.set()
        try:
            if self._hotkey is not None:
                self._hotkey.uninstall()
        except Exception:  # noqa: BLE001
            pass
        self._hotkey = None
        self._set_state("off")

    def reload(self) -> None:
        """配置改了之后重装钩子（关着就不动）。"""
        self.stop()
        if self._enabled():
            self.start()

    def _warmup_model(self) -> None:
        """后台线程预加载识别模型（不阻塞 start()；不占麦克风）。"""
        if self._engine is not None:
            return

        def _go():
            try:
                self.status.emit("正在准备识别模型…")
                self._load_models()
            except Exception:  # noqa: BLE001
                log.exception("预加载识别模型失败")

        threading.Thread(target=_go, name="VoiceModelWarmup", daemon=True).start()

    # ------------------------------------------------------------ 钩子回调（后台线程）
    def _on_pressed(self) -> None:
        """按住语音键开始录音（钩子线程回调 → 起录音线程）。"""
        if self._state == "recording" or self._rec_thread is not None:
            return
        if not self._enabled():
            return
        # 用 QTimer 投递回 GUI 线程再做（虽轻，但统一在 GUI 线程管理状态更稳）
        QTimer.singleShot(0, self._begin_recording)

    def _on_released(self) -> None:
        # 无论当前状态如何，松手一律收起「正在听你说话」提示——避免连按几次时
        # 因状态已切到 processing 导致某次松手被早退、提示一直挂在屏幕上。
        QTimer.singleShot(0, self._on_released_gui)

    def _on_released_gui(self) -> None:
        try:
            self._hide_listen_bubble()
        except Exception:  # noqa: BLE001
            log.debug("收起监听提示失败", exc_info=True)
        self._end_recording()

    # ------------------------------------------------------------ 录音
    def _begin_recording(self) -> None:
        if self._state == "recording":
            return
        self._set_state("recording")
        self.status.emit("正在听你说…（松开语音键结束）")
        # 在桌宠上方冒一个「正在听你说话」提示，让用户知道麦克风已在监听。
        # 松手时 _hide_listen_bubble 会主动收起；这里仍给一个有限时长兜底，
        # 万一收起调用被异常路径跳过，也会在 30 秒内自动消失，不会永久滞留。
        try:
            from . import runtime as rt

            rt.bubble("🎤 正在听你说话…", 30000)
        except Exception:  # noqa: BLE001
            log.debug("监听提示气泡失败", exc_info=True)
        self._rec_chunks = []
        self._rec_stop.clear()
        self._rec_thread = threading.Thread(
            target=self._record_loop, name="VoiceRecorder", daemon=True)
        self._rec_thread.start()

    def _record_loop(self) -> None:
        """后台线程：打开麦克风持续录音，直到 _rec_stop 被置位或超时。"""
        import numpy as np
        import sounddevice as sd

        try:
            # 不在这里加载模型（start() 已后台预热）。若还没就绪，录音照常先进行，
            # 识别阶段再等模型——避免模型加载的十几秒把用户说的话整个吞掉。
            # 端点已缓存就直接开流（省掉首次按键的逐端点试录延迟）；未缓存才探测。
            dev = getattr(self, "_usable_device", None)
            if dev is None:
                dev = self._pick_device()
            dvinfo = sd.query_devices(dev) if dev is not None else sd.query_devices(kind="input")
            sr = int(dvinfo.get("default_samplerate") or SAMPLE_RATE)
            block = max(1, int(sr * BLOCK_MS / 1000))
            stream = sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                                    blocksize=block, device=dev)
            stream.start()
            self._rec_stream = stream
            self._rec_dev_index = dev
            log.info("开始录音：device=%s samplerate=%d", dev, sr)
            max_blocks = int(MAX_RECORD_SEC * sr / block)
            n = 0
            while not self._rec_stop.is_set() and n < max_blocks:
                data, _of = stream.read(block)
                raw = data[:, 0].copy() if data.ndim > 1 else data.copy()
                self._rec_chunks.append((raw.astype(np.float32), sr))
                n += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("录音失败")
            self.error.emit(f"录音失败：{exc}")
        finally:
            try:
                if self._rec_stream is not None:
                    self._rec_stream.stop(); self._rec_stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._rec_stream = None
            # 关键：录音线程退出时清空句柄，否则下次按住会被 _on_pressed 的
            # 「_rec_thread is not None」守卫挡掉 → 「之后再按没反应」。
            self._rec_thread = None

    def _end_recording(self) -> None:
        """松开语音键：停录音 → 拼音频 → 后台识别 → 进对话。"""
        if self._state != "recording":
            return
        self._rec_stop.set()
        self._hide_listen_bubble()
        self._set_state("processing")
        self.status.emit("正在识别…")
        th = self._rec_thread
        if th is not None:
            th.join(timeout=3.0)  # 录音线程会很快退出（最多一个 block 的等待）
        chunks = list(self._rec_chunks)
        self._rec_chunks = []
        if not chunks:
            self._set_state("idle")
            self.status.emit("（没录到声音）按住「%s」再试一次～" % self.hotkey_combo())
            return
        threading.Thread(target=self._transcribe_and_dispatch, args=(chunks,),
                         name="VoiceTranscribe", daemon=True).start()

    def _hide_listen_bubble(self) -> None:
        """松开语音键时把「正在听你说话」提示收起来。"""
        try:
            from . import runtime as rt

            host = rt.bubble_host() if hasattr(rt, "bubble_host") else None
            if host is None:
                return
            # 宿主可能把气泡实现在 speech bubble 或 alert bubble 两处，
            # 两种收起方法都尝试一遍，确保提示一定消失。
            for name in ("hide_speech_bubble", "hide_bubble"):
                fn = getattr(host, name, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:  # noqa: BLE001
                        log.debug("%s 收起失败", name, exc_info=True)
        except Exception:  # noqa: BLE001
            log.debug("收起监听提示失败", exc_info=True)

    def _raw_name_candidates(self) -> list:
        """按配置的麦克风名返回【同名输入端点】若干候选，按托管层优先级排序。

        注意：这里只是「原始排序」，不代表一定能录到声音——DirectSound 在本机
        会「能开但 InputStream 永远返回全零」，WDM-KS「Blocking API not supported」。
        真正可用的端点由 :meth:`_pick_device` 用 InputStream 实测电平筛出来。
        排序只用于「相同情况下先试哪个」：WASAPI(3) > MME(2) > DirectSound(1) > 其它(0)。
        """
        name = str(self._cfg("companion_voice_device", "") or "").strip()
        try:
            import sounddevice as sd

            try:
                hostapis = sd.query_hostapis()
            except Exception:  # noqa: BLE001
                hostapis = []
            hits = []
            for idx, dev in enumerate(sd.query_devices()):
                ch = int(dev.get("max_input_channels", 0))
                if ch <= 0:
                    continue
                if name and name not in str(dev.get("name", "")):
                    continue
                try:
                    api = str(hostapis[dev.get("hostapi", -1)].get("name", "")).upper()
                except Exception:  # noqa: BLE001
                    api = ""
                if "WASAPI" in api:
                    tier = 3
                elif "MME" in api:
                    tier = 2
                elif "DIRECTSOUND" in api:
                    tier = 1
                else:  # WDM-KS 等
                    tier = 0
                hits.append((tier, ch, idx))
            if not hits:
                return [None]
            hits.sort(key=lambda t: (t[0], t[1]), reverse=True)
            return [t[2] for t in hits]
        except Exception:  # noqa: BLE001
            return [None]

    def _probe_input_stream(self, dev, seconds: float = 0.5):
        """用【程序实际录音用的 InputStream 路径】试录一小段，返回 (ok, rms, sr)。

        直接复用阻塞 read 的真实音频通路：WDM-KS 打不开、DirectSound 返回全零、
        设备被占用等都会被这里如实暴露，从而只在「真能拿到声音」的端点上录音。
        """
        import numpy as np
        import sounddevice as sd

        info = sd.query_devices(dev) if dev is not None else sd.query_devices(kind="input")
        sr = int(info.get("default_samplerate") or SAMPLE_RATE)
        block = max(1, int(sr * BLOCK_MS / 1000))
        stream = sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                                blocksize=block, device=dev)
        try:
            stream.start()
            n = max(1, int(seconds * sr / block))
            vals = []
            for _ in range(n):
                data, _of = stream.read(block)
                raw = data[:, 0] if data.ndim > 1 else data
                vals.append(np.asarray(raw, dtype=np.float32).copy())
            audio = np.concatenate(vals) if vals else np.zeros(0, dtype=np.float32)
            rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
            peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            return True, rms, peak, sr
        finally:
            try:
                stream.stop(); stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _pick_device(self) -> int | None:
        """挑一个【真能录到声音】的端点：按候选顺序用 InputStream 实测，取第一个能开的。

        - 之前「WASAPI 优先」的静态排序会踩到 DirectSound（InputStream 返回全零）
          或 WDM-KS（打不开）——这里改成真的开流试一下，开不了/全零就换下一个。
        - 选中后缓存到 self._usable_device，避免每次按键都重新探测。
        """
        cached = getattr(self, "_usable_device", None)
        cands = self._raw_name_candidates()
        order = []
        if cached is not None:
            order.append(cached)
        for c in cands:
            if c not in order:
                order.append(c)
        for dev in order:
            try:
                ok, rms, peak, sr = self._probe_input_stream(dev, seconds=0.2)
            except Exception as exc:  # noqa: BLE001
                log.info("麦克风端点 %s 打不开，跳过：%s", dev, exc)
                continue
            if not ok:
                continue
            # 允许「安静时电平为 0」——真正判断可用只看「能开且能读到数据」。
            self._usable_device = dev
            return dev
        log.warning("没有可用的麦克风输入端点（全部打不开或读不到数据）")
        return None

    def _demote_device(self, bad_dev) -> None:
        """把没听清的端点标记为可疑：下次首选换个端点（仅在还有别的候选时）。"""
        try:
            cands = self._raw_name_candidates()
            if len(cands) > 1 and bad_dev in cands:
                idx = cands.index(bad_dev)
                nxt = cands[(idx + 1) % len(cands)]
                self._usable_device = nxt
                log.info("麦克风端点 %s 未识别成功，下次改用端点 %s", bad_dev, nxt)
        except Exception:  # noqa: BLE001
            pass

    def _transcribe_and_dispatch(self, chunks) -> None:
        """后台线程：把录音拼起来 → 重采样到 16k → Whisper 识别 → 派发。"""
        import numpy as np

        try:
            # 按各自采样率把块重采样到 16k 后拼接
            pieces = []
            for raw, sr in chunks:
                if sr == SAMPLE_RATE:
                    pieces.append(raw)
                else:
                    rs = _resample_to_16k(raw, sr)
                    if rs.shape[0] > 0:
                        pieces.append(rs)
            if not pieces:
                self._set_state("idle")
                return
            audio = np.concatenate(pieces)
            # 太短（<0.2s）基本是误触
            if audio.shape[0] < int(0.2 * SAMPLE_RATE):
                self._set_state("idle")
                self.status.emit("（太短了）按住「%s」说完再松开～" % self.hotkey_combo())
                return
            rms = float(np.sqrt(np.mean(audio ** 2)) + 1e-9)
            log.info("录音结束：时长 %.2fs rms=%.4f", audio.shape[0] / SAMPLE_RATE, rms)

            # 电平太低基本是没说话/麦克风静音——直接提示，别硬塞给 Whisper 产生幻听。
            if rms < 0.004:
                self._set_state("idle")
                self.status.emit(
                    "（没收到声音）麦克风电平太低，请确认没静音、选对麦克风，"
                    "按住「%s」时再大声点说～" % self.hotkey_combo())
                return

            # 高通去低频嗡声（提升低信噪比录音可懂度、减少繁体误识），再按峰值做响度归一化。
            try:
                raw_rms = rms
                audio = _highpass(audio, SAMPLE_RATE)
                rms = float(np.sqrt(np.mean(audio ** 2)) + 1e-9)
                log.info("高通滤波：rms %.4f → %.4f", raw_rms, rms)
            except Exception:  # noqa: BLE001
                log.debug("高通滤波失败", exc_info=True)
            peak = float(np.max(np.abs(audio))) + 1e-9
            if peak < 0.9:
                gain = min(0.9 / peak, 20.0)  # 最多放大 20 倍，避免把噪声也炸开
                audio = (audio * gain).astype("float32")
                log.info("录音归一化：peak %.4f → x%.2f", peak, gain)

            if self._engine is None and not self._load_models():
                self._set_state("idle")
                return

            from . import asr

            beam = max(1, int(self._cfg("companion_whisper_beam", 2) or 2))
            # vad_filter=False：录音已经很短、且已按需门控，Whisper 内部 VAD 常把短的整句判成静音返回空。
            # initial_prompt 引导简体（否则中文模型常输出繁体，观感像「识别错」）。
            text = asr.transcribe(self._engine, audio, language="zh",
                                  vad_filter=False, beam_size=beam,
                                  initial_prompt=ZH_SIMPLIFIED_PROMPT)
            self.heard.emit(text or "")
            text = (text or "").strip()
            log.info("识别结果：%r", text)
            used_dev = self._rec_dev_index
            if (not text or _looks_like_subtitle_junk(text)
                    or _is_prompt_echo(text) or _looks_hallucinated(text)):
                self._set_state("idle")
                # 只在「完全没识别到」时才降级端点；提示词回声/幻听说明端点其实有声音、
                # 只是这段太轻或没说话，不应该因此判定端点坏、更不能每段都降级。
                if not text:
                    self._demote_device(used_dev)
                    self.status.emit("（没听清）再按住「%s」说一遍～" % self.hotkey_combo())
                else:
                    self.status.emit("（这段没听清）再按住说一遍试试～")
                return
            # 识别成功 → 记住这个端点，下次优先用它
            if used_dev is not None:
                self._last_good_device = used_dev
            self._last_user_text = text
            # 从后台识别线程投递回 GUI 线程派发（不能用 QTimer.singleShot，
            # 后台线程没有 Qt 事件循环，单发定时器永远不触发 → 识别成功却没进对话）。
            self.dispatch_requested.emit(text)
        except Exception as exc:  # noqa: BLE001
            log.exception("识别失败")
            self.error.emit(f"识别失败：{exc}")
            self._set_state("idle")

    def _dispatch(self, text: str) -> None:
        """在 GUI 线程里按内容派发：指令 / 普通对话。"""
        # 兜底：防止 initial_prompt 回声/字幕套话幻听漏网送进对话
        if _is_prompt_echo(text) or _looks_like_subtitle_junk(text):
            log.info("丢弃疑似提示词回声/幻听：%r", text)
            self._set_state("idle")
            return
        if self._state == "confirm":
            self._handle_confirm(text)
            return
        # 自拍指令先二次确认
        if _contains_any(text, SELFIE_WORDS):
            self._ask_selfie()
            return
        if _contains_any(text, HOME_WORDS):
            self._do_home()
            self._set_state("idle")
            return
        if _contains_any(text, WALK_WORDS):
            self._do_walk()
            self._set_state("idle")
            return
        # 普通对话：发进 AI 对话（会自动调用 LLM 生成回复并 TTS 朗读）
        self._send_to_chat(text)

    # ------------------------------------------------------------ 模型
    def _load_models(self) -> bool:
        """在后台线程加载识别模型（推挽式只需要一个对话/识别模型）。"""
        from . import asr

        model_dir = asr.resolve_model_dir(self._cfg)
        if not model_dir:
            self.error.emit(
                "还没有可用的 Whisper 模型：请到「陪伴扩展设置 → 语音助手」里"
                "选一个模型点「下载」，或填入已有模型目录。")
            return False
        try:
            device = str(self._cfg("companion_whisper_device", "cpu") or "cpu")
            compute = str(self._cfg("companion_whisper_compute", "int8") or "int8")
            self._engine = asr.load_engine(model_dir, device=device, compute_type=compute)
            self._model_main_info = str(model_dir)
            try:
                eff_cuda = bool(getattr(asr, "_cuda_available", None))
            except Exception:
                eff_cuda = False
            eff_note = "cuda" if eff_cuda else ("cpu（CUDA 不可用已自动回退，不影响使用）"
                                               if device != "cpu" else "cpu")
            log.info("语音识别模型已加载：%s（请求 device=%s,%s 实际=%s）",
                     model_dir, device, compute, eff_note)
            return True
        except Exception as exc:  # noqa: BLE001
            log.exception("加载 Whisper 模型失败")
            self.error.emit(f"加载模型失败：{exc}")
            return False

    def model_info(self) -> dict:
        return {"dialog": getattr(self, "_model_main_info", "") or "",
                "wake": ""}

    # ------------------------------------------------------------ 指令
    def _ask_selfie(self) -> None:
        self._pending_selfie = True
        self._set_state("confirm")
        self._say("要自拍一张吗？说「确认」我就拍，说「不用」就取消。")
        self.status.emit("等待确认自拍：说「确认」或「不用」")

    def _handle_confirm(self, text: str) -> None:
        if _contains_any(text, CANCEL_WORDS):
            self._pending_selfie = False
            self._set_state("idle")
            self._say("好，那不拍了。")
            return
        if _contains_any(text, CONFIRM_WORDS):
            self._pending_selfie = False
            self._set_state("idle")
            self._say("好嘞，拍拍看～")
            self._do_selfie()
            return
        # 没听清 → 保持 confirm，再问一次
        self._say("要拍的话说「确认」，不想拍说「不用」哦。")

    def _do_selfie(self) -> None:
        def _go():
            try:
                from . import runtime

                svc = runtime.service()
                if svc is not None:
                    svc.take_selfie("voice")
            except Exception:  # noqa: BLE001
                log.exception("语音触发自拍失败")

        QTimer.singleShot(0, _go)

    def _do_walk(self) -> None:
        def _go():
            try:
                from . import runtime

                svc = runtime.service()
                if svc is None:
                    return
                if getattr(svc, "walking", False):
                    self._say("我已经在外面啦～")
                    return
                svc.toggle()
            except Exception:  # noqa: BLE001
                log.exception("语音触发溜达失败")

        QTimer.singleShot(0, _go)

    def _do_home(self) -> None:
        def _go():
            try:
                from . import runtime

                svc = runtime.service()
                if svc is None:
                    return
                if not getattr(svc, "walking", False):
                    self._say("我现在就在家呀～")
                    return
                svc.toggle()
            except Exception:  # noqa: BLE001
                log.exception("语音触发回家失败")

        QTimer.singleShot(0, _go)

    # ------------------------------------------------------------ 发进对话
    def _send_to_chat(self, text: str) -> None:
        def _go():
            try:
                from . import runtime

                app = runtime.app()
                if app is None:
                    self._set_state("idle")
                    return
                window = self._ensure_chat(app)
                if window is None:
                    self._set_state("idle")
                    self._say("对话窗口打不开，我先记下你这句。")
                    return
                window.voice_submit(text)
                self._set_state("idle")
            except Exception:  # noqa: BLE001
                self._set_state("idle")
                log.exception("语音消息发送到对话失败")

        QTimer.singleShot(0, _go)

    def _ensure_chat(self, app, silent: bool = True):
        """拿到/打开 AI 对话窗。

        兼容三种宿主形态（runtime.app() 实际返回什么就按什么处理）：
        - App：有 open_modern_chat / modern_chat_window 直接可用；
        - AppShell（overlay 拓扑下 runtime.app() 返回的进程级壳）：它没有
          open_modern_chat，聊天窗挂在它主窗实例 app.instance 上（PetInstance，
          有 open_chat / modern_chat_window / chat_window），也可能要退到
          app._hotkey_overlay() 的 open_quick_chat；
        - 兜底：任何带 open_chat 的对象。

        ``silent=True``（语音注入默认）：只建窗/复用窗、驱动生成，**不 show/
        raise/activate**，不在前台弹出聊天窗。生成由 ``_begin_generation`` 驱动，
        与窗口可见性无关；回复语音照常朗读。
        """
        def _win_from(obj):
            if obj is None:
                return None
            for attr in ("modern_chat_window", "chat_window", "legacy_chat_window"):
                w = getattr(obj, attr, None)
                if w is None:
                    continue
                try:
                    import shiboken6
                    if shiboken6.isValid(w):
                        return w
                except Exception:  # noqa: BLE001
                    return w
            return None

        # 收集所有可能的「持有聊天窗 / 能打开聊天窗」的候选对象
        cands = [app, getattr(app, "instance", None),
                 getattr(app, "_instance", None)]

        # 1) 已经有可用的聊天窗就直接返回
        for obj in cands:
            w = _win_from(obj)
            if w is not None:
                return w

        # 2) 逐候选按其拥有的方法打开聊天窗
        opened = False
        for obj in cands:
            if obj is None:
                continue
            # silent 优先：只有 open_modern_chat 支持 silent 参数（不弹前台窗）
            modern = getattr(obj, "open_modern_chat", None)
            if silent and callable(modern):
                try:
                    modern(silent=True)
                    opened = True
                except TypeError:
                    # 旧签名不接受 silent，退回普通调用
                    try:
                        modern()
                        opened = True
                    except Exception:  # noqa: BLE001
                        log.exception("打开聊天窗失败（%s.open_modern_chat）", type(obj).__name__)
                except Exception:  # noqa: BLE001
                    log.exception("打开聊天窗失败（%s.open_modern_chat silent）", type(obj).__name__)
                if opened:
                    break
                continue
            for name in ("open_chat", "open_modern_chat", "open_full_chat"):
                fn = getattr(obj, name, None)
                if callable(fn):
                    try:
                        fn()
                        opened = True
                    except Exception:  # noqa: BLE001
                        log.exception("打开聊天窗失败（%s.%s）", type(obj).__name__, name)
                    break
            if opened:
                break

        # 3) AppShell 的 overlay 回退：快速对话气泡
        if not opened:
            try:
                ov = app._hotkey_overlay() if hasattr(app, "_hotkey_overlay") else None
                quick = getattr(ov, "open_quick_chat", None) if ov is not None else None
                if callable(quick):
                    quick()
                    log.info("已用快速对话气泡作为聊天入口（overlay 回退）")
                    opened = True
            except Exception:  # noqa: BLE001
                log.exception("overlay 快速对话打开失败")

        if not opened:
            log.warning("当前宿主没有可用的打开聊天窗方法：%r", type(app).__name__)

        # 4) 打开后再取窗口
        for obj in cands:
            w = _win_from(obj)
            if w is not None:
                return w
        return None

    # ------------------------------------------------------------ 出声
    def _say(self, text: str) -> None:
        """念一句（可选 TTS）+ 冒气泡。用信号跨线程投递到 GUI 线程执行。"""
        text = str(text or "").strip()
        if not text:
            return
        try:
            self.speak_requested.emit(text)
        except Exception:  # noqa: BLE001
            self._do_speak(text)

    def _do_speak(self, text: str) -> None:
        """真正发声（GUI 线程）：冒气泡 + 本地 TTS + 估算静音窗口。"""
        if not text:
            return
        try:
            from . import runtime as rt

            rt.bubble(text, 6000)
        except Exception:  # noqa: BLE001
            log.debug("语音助手气泡失败", exc_info=True)
        try:
            from . import tts as tts_mod

            store = self._store()
            if store is None:
                return
            ok = tts_mod.speak_text(text, store, kind="chat")
            if ok:
                dur = max(1.2, min(8.0, len(text) / 5.0 + 0.6))
                self._mute_until = max(self._mute_until, time.time() + dur)
        except Exception:  # noqa: BLE001
            log.debug("语音助手出声失败", exc_info=True)

    def _is_muted(self) -> bool:
        """是否需要丢弃当前麦克风数据（只在「她自己出声」的短窗口内）。"""
        now = time.time()
        if self._mute_until and now > self._mute_until + 30.0:
            self._mute_until = 0.0
        return bool(now < self._mute_until)

    # ------------------------------------------------------------ 状态
    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        try:
            self.state_changed.emit(state)
        except Exception:  # noqa: BLE001
            pass


__all__ = ["VoiceAssistant", "VoiceAssistantError", "SAMPLE_RATE"]
