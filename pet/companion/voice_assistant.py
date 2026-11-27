# -*- coding: utf-8 -*-
"""语音助手：麦克风监听 + 唤醒词/结束词快速识别 + 对话模式 + 语音指令。

工作方式（仿智能音箱）
----------------------
1. **常驻监听**：后台线程用 sounddevice 持续采集 16kHz 单声道，按 Silero VAD
   把「有人在说话」的片段切出来（静音不计）。
2. **待命（IDLE）**：每个语音片段用最快的 tiny 模型只做「关键词匹配」——
   判断里面有没有唤醒词（默认「小鱼小鱼」）。命中就进入对话模式。这一步
   只跑一个几十毫秒的短音频小模型，不做整句转写，响应快。
3. **对话模式（DIALOG）**：进入后每段话用主模型（默认 base）完整转写，然后：
   * 命中结束词（默认「休息一下」）→ 退出对话模式，回到待命。
   * 命中「自拍」类词 → **二次确认**：先问她要不要自拍，用户说「确认」才拍。
   * 命中「溜达」类词 → 出去溜达；命中「回家」类词 → 叫她回家（会回来发自拍）。
   * 其它 → 当成打字一样发进 AI 对话，程序自动回复（对话记录照常保存）。
4. 所有合成/播报复用已有的本地 TTS 通道（tts.speak_text），没有 TTS 就只用气泡。

设计要点
--------
* 唤醒/结束词识别「不调大模型整句转写」：用 tiny 模型 + 关键词模糊包含判断，
  真·轻量。若用户把「唤醒词识别模型」设成和主模型一样，也照常工作（只是慢些）。
* 全部线程安全：麦克风采集在 QThread，状态机在采集线程内串行处理（同一时间
  只处理一段话），对 UI 的通知走 Qt 信号（队列投递回 GUI 线程）。
* 关闭总开关时 ``stop()`` 停线程、关音频流，绝不占麦克风。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

from PySide6.QtCore import QObject, Signal, QTimer

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK_MS = 30                      # 每次读 30ms 音频
BLOCK = int(SAMPLE_RATE * BLOCK_MS / 1000)

# 唤醒/结束词匹配：去掉标点空格后的「包含」判断（允许模型多吐几个字）
_PUNCT = re.compile(r"[\s，。！？、,.!?\-—…~·\"'“”‘’《》()（）\[\]【】:：;；]+")

# 默认词表（可被配置覆盖）
DEFAULT_WAKE = "小鱼小鱼"
DEFAULT_SLEEP = "休息一下"

# 指令关键词（同义词组）
SELFIE_WORDS = ["自拍", "拍一张", "拍个照", "拍照", "来张照片", "来个自拍", "拍张照"]
WALK_WORDS = ["溜达", "散步", "出去走走", "出门走走", "逛街", "出去逛", "走走"]
HOME_WORDS = ["回家", "回来", "回来吧", "该回家了", "回窝"]
CONFIRM_WORDS = ["确认", "确定", "好的", "好", "是", "可以", "行", "嗯", "对", "拍吧", "来"]
CANCEL_WORDS = ["不用", "算了", "取消", "不要", "别", "不了", "否", "不是"]


def _norm(text: str) -> str:
    return _PUNCT.sub("", str(text or "")).lower()


def _contains_any(text: str, words: list[str]) -> str:
    """返回第一个命中的词（已归一化匹配）；没有返回空串。"""
    norm = _norm(text)
    for w in words:
        if _norm(w) and _norm(w) in norm:
            return w
    return ""


class VoiceAssistantError(RuntimeError):
    pass


def list_input_devices() -> list[tuple[int, str, int]]:
    """返回所有输入设备 [(序号, 设备名, 输入声道数)]，供设置页下拉/测试用。"""
    out: list[tuple[int, str, int]] = []
    try:
        import sounddevice as sd

        for idx, dev in enumerate(sd.query_devices()):
            if int(dev.get("max_input_channels", 0)) > 0:
                out.append((idx, str(dev.get("name", "")), int(dev.get("max_input_channels", 0))))
    except Exception:  # noqa: BLE001
        pass
    return out


def probe_mic(device_name: str = "", seconds: float = 3.0, on_level=None) -> dict:
    """测试某个麦克风能否正常收音。

    返回 {"ok":bool, "rms":float, "peak":float, "sr":int, "device":int|None, "error":str}。
    ``on_level`` 每次收到一小段会回调 rms（用于界面实时波形/音量条）。
    会自动按设备默认采样率打开（USB 麦常不支持 16000Hz），并挑选同名设备里
    真正有电平的 WASAPI 端点。
    """
    import numpy as np

    result = {"ok": False, "rms": 0.0, "peak": 0.0, "sr": SAMPLE_RATE, "device": None, "error": ""}
    try:
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"没有 sounddevice：{exc}"
        return result

    # 选出候选设备序号
    candidates: list[int] = []
    name = str(device_name or "").strip()
    if name:
        try:
            hostapis = sd.query_hostapis()
        except Exception:  # noqa: BLE001
            hostapis = []
        hits = []
        for idx, dev in enumerate(sd.query_devices()):
            if int(dev.get("max_input_channels", 0)) > 0 and name in str(dev.get("name", "")):
                try:
                    api = str(hostapis[dev.get("hostapi", -1)].get("name", ""))
                except Exception:  # noqa: BLE001
                    api = ""
                hits.append((idx, api, int(dev.get("max_input_channels", 0))))
        hits.sort(key=lambda t: (1 if "WASAPI" in t[1].upper() else 0,
                                 0 if "MME" in t[1].upper() else 1, t[2], t[0]), reverse=True)
        candidates = [h[0] for h in hits]
    if not candidates:
        candidates = [None]  # 系统默认

    total_best = -1.0
    for dev in candidates:
        try:
            info = sd.query_devices(dev) if dev is not None else sd.query_devices(kind="input")
            sr = int(info.get("default_samplerate") or SAMPLE_RATE)
        except Exception:  # noqa: BLE001
            sr = SAMPLE_RATE
        block = max(1, int(sr * 0.05))
        try:
            peak = 0.0
            acc: list[float] = []
            with sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                                blocksize=block, device=dev) as stream:
                need = int(seconds * sr / block)
                for _ in range(max(1, need)):
                    data, _ov = stream.read(block)
                    x = data[:, 0] if data.ndim > 1 else data
                    rms = float(np.sqrt(np.mean(x.astype(np.float32) ** 2)) + 1e-12)
                    peak = max(peak, float(np.max(np.abs(x))) if x.size else 0.0)
                    acc.append(rms)
                    if on_level:
                        try:
                            on_level(rms)
                        except Exception:  # noqa: BLE001
                            pass
            mean_rms = float(np.mean(acc)) if acc else 0.0
            if mean_rms > total_best:
                total_best = mean_rms
                result.update({"ok": mean_rms > 0.001 or peak > 0.02, "rms": mean_rms,
                               "peak": peak, "sr": sr, "device": dev, "error": ""})
            if result["ok"]:
                break
        except Exception as exc:  # noqa: BLE001
            if not result["error"]:
                result["error"] = f"打开设备 {dev} 失败：{exc}"
            continue
    if not result["ok"] and not result["error"]:
        result["error"] = "麦克风几乎没有声音（rms≈0），请检查是否静音、选错设备或在系统里禁用了"
    return result


class VoiceAssistant(QObject):
    """麦克风语音助手（单例由 runtime 持有）。"""

    # 状态/提示给 UI（气泡文字）
    status = Signal(str)                 # 给状态栏 / 气泡的一句话
    state_changed = Signal(str)          # "idle" | "dialog" | "confirm" | "off"
    woke = Signal()                      # 被唤醒
    slept = Signal()                     # 退出对话
    heard = Signal(str)                  # 识别到的整句（调试/显示）
    error = Signal(str)

    def __init__(self, app, parent=None) -> None:
        super().__init__(parent)
        self._app = app
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stream = None
        self._engine = None          # 主识别引擎
        self._kws_engine = None      # 唤醒词识别引擎（通常 tiny）
        self._state = "off"
        self._pending_selfie = False  # 二次确认中
        self._last_active = 0.0       # 最后一次听到说话的时间（对话超时用）
        self._device_index: int | None = None
        self._restart_pending = False  # 旧线程退出后是否需要自动重启（reload 用）

    # ------------------------------------------------------------ 配置读取
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

    @property
    def state(self) -> str:
        return self._state

    # ------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        """启动监听。返回 True 表示已在跑。

        **绝不在调用线程（通常是 GUI 主线程）里加载模型**：Whisper 模型加载、
        CUDA 探活回退都要好几秒，放在主线程会让整个程序「未响应」——之前用户
        只要点「应用并重启监听」或保存配置就卡死，根子就在这。现在这里只做
        轻量检查 + 起后台线程，真正的模型加载在 ``_run`` 里完成，UI 立刻返回。
        """
        if self._thread is not None and self._thread.is_alive():
            return True
        if not self._enabled():
            self._set_state("off")
            return False
        try:
            import sounddevice  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"缺少音频库 sounddevice：{exc}")
            return False

        self._device_index = self._resolve_device()
        self._stop.clear()
        self._pending_selfie = False
        self._last_active = time.time()
        self._engine = None
        self._kws_engine = None
        self._thread = threading.Thread(target=self._run, name="VoiceAssistant", daemon=True)
        self._thread.start()
        self._set_state("idle")
        self.status.emit("语音助手正在启动…（首次加载模型可能要等几秒）")
        return True

    def _load_models(self) -> bool:
        """在后台线程里加载主模型 + 唤醒词模型。失败发 error 并返回 False。"""
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
            log.info("语音助手主模型已加载：%s（device=%s,%s）", model_dir, device, compute)
            # 唤醒词识别模型：默认 tiny（快）；若用户设了就用它
            kws_path = str(self._cfg("companion_whisper_kws_model_path", "") or "").strip()
            if kws_path:
                self._kws_engine = asr.load_engine(kws_path, device=device, compute_type=compute)
            else:
                tiny_dir = self._find_tiny(model_dir)
                if tiny_dir:
                    self._kws_engine = asr.load_engine(tiny_dir, device=device, compute_type="int8")
                    log.info("唤醒词模型 tiny：%s", tiny_dir)
                else:
                    self._kws_engine = self._engine
                    log.info("未找到 tiny，唤醒词改用主模型识别")
            return True
        except Exception as exc:  # noqa: BLE001
            log.exception("加载 Whisper 模型失败")
            self.error.emit(f"加载模型失败：{exc}")
            return False

    def _find_tiny(self, fallback_main: str) -> str:
        """找 tiny 模型目录；没有就返回空（用主引擎兜底）。"""
        try:
            from . import asr

            for item in asr.scan_local_models([self._cfg("companion_whisper_download_dir", "")]):
                if item["size"] == "tiny":
                    return item["path"]
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _candidate_devices(self) -> list[int | None]:
        """按配置的麦克风名找出候选设备序号（按优先级排序）。

        **关键**：同名麦克风在 Windows 上常被不同 host API 暴露成多个设备序号
        （MME/DirectSound/WASAPI/WDM-KS…）。其中 MME 那个经常「能开但永远是静音」，
        WASAPI 才有真电平但偶尔会抛 AUDCLNT_E_OUT_OF_ORDER，WDM-KS 不支持阻塞读。
        旧代码取第一个子串命中，正好命中那个永远静音的 MME 端点 → 录到永远是 0，
        唤醒词自然识别不出来。这里返回**全部同名候选**（WASAPI 优先、MME 靠后），
        由 _run 逐个尝试、哪个能开且能读就先用哪个。名字为空/找不到 → [None] 走默认。
        """
        name = str(self._cfg("companion_voice_device", "") or "").strip()
        if not name:
            return [None]
        try:
            import sounddevice as sd

            try:
                hostapis = sd.query_hostapis()
            except Exception:  # noqa: BLE001
                hostapis = []
            hits: list[tuple[int, str, int]] = []
            for idx, dev in enumerate(sd.query_devices()):
                if int(dev.get("max_input_channels", 0)) > 0 and name in str(dev.get("name", "")):
                    try:
                        api = str(hostapis[dev.get("hostapi", -1)].get("name", ""))
                    except Exception:  # noqa: BLE001
                        api = ""
                    hits.append((idx, api, int(dev.get("max_input_channels", 0))))
            if not hits:
                return [None]
            hits.sort(key=lambda t: (1 if "WASAPI" in t[1].upper() else 0,
                                     0 if "MME" in t[1].upper() else 1, t[2], t[0]), reverse=True)
            log.info("麦克风同名设备 %d 个，候选序号=%s", len(hits), [h[0] for h in hits])
            return [h[0] for h in hits]
        except Exception:  # noqa: BLE001
            pass
        return [None]

    def _resolve_device(self) -> int | None:
        """兼容旧接口：返回首选设备序号。"""
        cands = self._candidate_devices()
        return cands[0] if cands else None

    def stop(self) -> None:
        """请求停止。**不 join**（避免在 GUI 主线程上阻塞），后台线程看到
        ``_stop`` 后会自行关流退出；模型加载中也不影响——加载完会立即发现
        ``_stop`` 已置位而直接退出。"""
        self._stop.set()
        self._set_state("off")

    def reload(self) -> None:
        """配置改了之后重来一遍（关着就不动）。

        全程不在调用线程上等待：置 _stop 后交给后台线程退出，线程在 finally
        里发现 ``_restart_pending`` 就通过 QTimer 回到 GUI 线程重新 start()。
        """
        if not self._enabled():
            self._restart_pending = False
            self.stop()
            return
        th = self._thread
        if th is not None and th.is_alive():
            # 旧线程还在（可能在加载模型）：标记重启，等它自己退出后再拉起
            self._restart_pending = True
            self._stop.set()
            self._set_state("off")
        else:
            # 没有在跑，直接（非阻塞地）启动
            self._restart_pending = False
            self.start()

    # ------------------------------------------------------------ 主循环
    def _run(self) -> None:
        import numpy as np
        import sounddevice as sd
        from faster_whisper.vad import get_speech_timestamps, VadOptions

        # 1) 先在后台线程加载模型（这里慢没关系，不卡 UI）
        self.status.emit("语音助手：正在加载模型…")
        try:
            ok = self._load_models()
        except Exception:  # noqa: BLE001
            log.exception("加载模型异常")
            ok = False
        if not ok or self._stop.is_set():
            self._set_state("off")
            self._thread = None
            if getattr(self, "_restart_pending", False):
                self._restart_pending = False
                try:
                    QTimer.singleShot(300, self.start)
                except Exception:  # noqa: BLE001
                    pass
            return

        self._last_active = time.time()
        self.status.emit("语音助手已开启，说「%s」叫醒我～" % self._wake_word())

        vad_opts = VadOptions(
            threshold=0.5,
            min_speech_duration_ms=250,
            min_silence_duration_ms=500,
            speech_pad_ms=120,
        )
        buffer = np.zeros(0, dtype=np.float32)
        silence_blocks = 0
        speaking = False
        silence_limit = int(0.6 / (BLOCK_MS / 1000.0))  # 0.6 秒静音判定一句话结束
        max_seg_sec = 15.0

        # 自适应能量门限：先按一个较低初值，持续跟踪底噪（静音块 RMS 的滑窗分位）
        energy_threshold = 0.004
        noise_floor = 0.0
        last_level_log = 0.0
        # 采集采样率：很多 USB 麦克风/虚拟声卡不支持 16000Hz（强行给 16000 会
        # 报 Invalid sample rate -9997 直接开流失败）。这里按设备自身默认采样率
        # 打开，再在每个块上就近重采样到 SAMPLE_RATE(16000) 交给 VAD/whisper。
        def _open_stream(dev):
            dvinfo = sd.query_devices(dev) if dev is not None else sd.query_devices(kind="input")
            sr = int(dvinfo.get("default_samplerate") or SAMPLE_RATE)
            stream = sd.InputStream(
                samplerate=sr, channels=1, dtype="float32",
                blocksize=max(1, int(sr * BLOCK_MS / 1000)), device=dev,
            )
            stream.start()
            # 试读一小段：WASAPI 常见的 AUDCLNT_E_OUT_OF_ORDER 往往在首读才暴雷，
            # 这里先探一下，坏就丢弃换下一个候选。
            stream.read(max(1, int(sr * BLOCK_MS / 1000)))
            return stream, sr

        candidates = self._candidate_devices()
        self._stream = None
        capture_sr = SAMPLE_RATE
        open_err = ""
        for dev in candidates:
            if self._stop.is_set():
                break
            try:
                self._stream, capture_sr = _open_stream(dev)
                self._device_index = dev
                log.info("麦克风已打开：device=%s samplerate=%d", dev, capture_sr)
                break
            except Exception as exc:  # noqa: BLE001
                open_err = str(exc)
                log.warning("打开设备 %s 失败，尝试下一个候选：%s", dev, exc)
                try:
                    if self._stream is not None:
                        self._stream.close()
                except Exception:  # noqa: BLE001
                    pass
                self._stream = None
                continue
        if self._stream is None:
            log.warning("所有麦克风候选都打不开：%s", open_err)
            self.error.emit(f"打不开麦克风：{open_err or '没有可用输入设备'}")
            self._set_state("off")
            self._thread = None
            if getattr(self, "_restart_pending", False):
                self._restart_pending = False
                try:
                    QTimer.singleShot(300, self.start)
                except Exception:  # noqa: BLE001
                    pass
            return

        capture_block = max(1, int(capture_sr * BLOCK_MS / 1000))

        def _resample_to_16k(x):
            n = x.shape[0]
            need = int(round(n * SAMPLE_RATE / capture_sr))
            if need <= 0:
                return np.zeros(0, dtype=np.float32)
            src = np.linspace(0.0, 1.0, n, endpoint=False, dtype=np.float32)
            dst = np.linspace(0.0, 1.0, need, endpoint=False, dtype=np.float32)
            return np.interp(dst, src, x).astype(np.float32)

        read_fail = 0
        try:
            while not self._stop.is_set():
                try:
                    data, overflowed = self._stream.read(capture_block)
                    read_fail = 0
                except Exception:  # noqa: BLE001
                    read_fail += 1
                    log.warning("读取麦克风失败（第 %d 次）", read_fail)
                    if read_fail >= 5:
                        log.exception("连续读取麦克风失败，退出")
                        break
                    # 尝试重开当前流（WASAPI 偶发 OUT_OF_ORDER，重开可恢复）
                    try:
                        self._stream.stop(); self._stream.close()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        self._stream, capture_sr = _open_stream(self._device_index)
                        capture_block = max(1, int(capture_sr * BLOCK_MS / 1000))
                    except Exception:  # noqa: BLE001
                        time.sleep(0.3)
                    continue
                raw = data[:, 0].copy() if data.ndim > 1 else data.copy()
                chunk = _resample_to_16k(raw) if capture_sr != SAMPLE_RATE else raw.astype(np.float32)
                buffer = np.concatenate([buffer, chunk])
                rms = float(np.sqrt(np.mean(raw ** 2)) + 1e-9)

                # 自适应门限：把底噪推高到当前 RMS 的平滑值，门限 = 底噪 * 3 且不小于 0.003
                if rms < energy_threshold:
                    noise_floor = 0.98 * noise_floor + 0.02 * rms
                    energy_threshold = max(0.003, min(0.02, noise_floor * 3.0 + 0.002))
                speech = rms > energy_threshold

                # 每 3 秒打一条电平日志，方便排查「麦克风到底有没有信号」
                now = time.time()
                if now - last_level_log > 3.0:
                    last_level_log = now
                    log.info("麦克风电平 rms=%.4f 门限=%.4f 状态=%s", rms, energy_threshold, self._state)

                if speech:
                    speaking = True
                    silence_blocks = 0
                else:
                    if speaking:
                        silence_blocks += 1
                seg_too_long = buffer.shape[0] > int(max_seg_sec * SAMPLE_RATE)
                seg_done = speaking and silence_blocks >= silence_limit
                if not (seg_done or seg_too_long):
                    continue
                # 一段话结束：取出有语音的部分
                audio = buffer.copy()
                buffer = np.zeros(0, dtype=np.float32)
                speaking = False
                silence_blocks = 0
                if audio.shape[0] < int(0.3 * SAMPLE_RATE):
                    continue
                # Silero VAD 精修（去掉首尾/中间静音）
                try:
                    ts = get_speech_timestamps(audio, vad_options=vad_opts)
                    if ts:
                        a = ts[0]["start"]; b = ts[-1]["end"]
                        audio = audio[a:b]
                except Exception:  # noqa: BLE001
                    pass
                if audio.shape[0] < int(0.25 * SAMPLE_RATE):
                    continue
                try:
                    self._handle_segment(audio)
                except Exception:  # noqa: BLE001
                    log.exception("处理语音片段失败")
        finally:
            try:
                if self._stream is not None:
                    self._stream.stop(); self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None
            self._thread = None
            # reload 请求过重启：回到 GUI 线程重新 start()（含重新读配置）
            if getattr(self, "_restart_pending", False):
                self._restart_pending = False
                try:
                    QTimer.singleShot(300, self.start)
                except Exception:  # noqa: BLE001
                    log.exception("安排语音助手重启失败")

    # ------------------------------------------------------------ 状态机
    def _wake_word(self) -> str:
        return str(self._cfg("companion_voice_wake_word", DEFAULT_WAKE) or DEFAULT_WAKE)

    def _sleep_word(self) -> str:
        return str(self._cfg("companion_voice_sleep_word", DEFAULT_SLEEP) or DEFAULT_SLEEP)

    def _handle_segment(self, audio) -> None:
        from . import asr

        # 对话模式空闲超时（默认 60 秒没说话就自动退出，避免一直占着）
        timeout = float(self._cfg("companion_voice_dialog_timeout", 60) or 60)
        if self._state in ("dialog", "confirm") and timeout > 0:
            if time.time() - self._last_active > timeout:
                self._exit_dialog(reason="超时")
                return

        if self._state == "idle":
            # 只做唤醒词匹配（用 KWS 小模型，短促）
            text = asr.transcribe(self._kws_engine, audio, language="zh",
                                  vad_filter=False, beam_size=1)
            self.heard.emit(f"[待命] {text}")
            if _contains_any(text, [self._wake_word()]) or self._fuzzy_wake(text):
                self._enter_dialog()
            return

        # 对话 / 确认模式：完整转写
        self._last_active = time.time()
        text = asr.transcribe(self._engine, audio, language="zh",
                              vad_filter=True, beam_size=int(self._cfg("companion_whisper_beam", 1) or 1))
        self.heard.emit(text)
        if not text.strip():
            return

        # 结束词
        if _contains_any(text, [self._sleep_word()]):
            self._exit_dialog(reason="用户说休息")
            return

        if self._state == "confirm":
            self._handle_confirm(text)
            return

        # DIALOG 普通对话
        self._handle_dialog_text(text)

    def _fuzzy_wake(self, text: str) -> bool:
        """唤醒词模糊匹配：模型把「小鱼小鱼」识别成「小鱼小玉」之类也认。

        规则：归一化后，唤醒词去重后的字大部分出现在句子里，且间隔不太远。
        """
        wake = _norm(self._wake_word())
        norm = _norm(text)
        if not wake or not norm:
            return False
        # 逐字按顺序匹配的比例
        j = 0
        hit = 0
        for ch in norm:
            if j < len(wake) and ch == wake[j]:
                hit += 1
                j += 1
        # 「小鱼小鱼」共 4 字，命中 >= 3 且含「鱼」视为命中
        base = wake.replace(wake[0], "", 1) if len(wake) > 1 else wake
        return hit >= max(2, len(wake) - 1) or (base and _norm(base) in norm)

    def _enter_dialog(self) -> None:
        self._last_active = time.time()
        self._set_state("dialog")
        self.woke.emit()
        self._greet()
        self.status.emit("已唤醒，正在对话中（说「%s」结束）" % self._sleep_word())

    def _greet(self) -> None:
        """被唤醒的正反馈：说一段打招呼的话 + 让桌宠做一个动画动作。"""
        import random

        lines = (
            "嗯，我在听～",
            "诶？叫我啦，我在～",
            "来啦来啦，想聊点什么呀？",
            "在呢在呢，你说～",
        )
        self._say(random.choice(lines))

        def _anim():
            try:
                from . import runtime

                svc = runtime.service()
                if svc is not None and hasattr(svc, "greet_animation"):
                    svc.greet_animation()
            except Exception:  # noqa: BLE001
                log.debug("语音唤醒动画失败", exc_info=True)

        QTimer.singleShot(0, _anim)

    def _exit_dialog(self, reason: str = "") -> None:
        self._pending_selfie = False
        self._set_state("idle")
        self.slept.emit()
        self._say("好，我先休息啦～")
        self.status.emit("对话结束%s，说「%s」可以再叫醒我" % (("（%s）" % reason) if reason else "", self._wake_word()))

    def _handle_dialog_text(self, text: str) -> None:
        # 回家优先于溜达（「回家」里不含溜达词，但保险起见先判回家）
        if _contains_any(text, HOME_WORDS):
            self._do_home()
            return
        if _contains_any(text, WALK_WORDS):
            self._do_walk()
            return
        if _contains_any(text, SELFIE_WORDS):
            self._ask_selfie()
            return
        # 普通聊天：发进 AI 对话
        self._send_to_chat(text)

    # ------------------------------------------------------------ 指令
    def _ask_selfie(self) -> None:
        self._pending_selfie = True
        self._set_state("confirm")
        self._say("要自拍一张吗？说「确认」我就拍，说「不用」就取消。")
        self.status.emit("等待确认自拍：说「确认」或「不用」")

    def _handle_confirm(self, text: str) -> None:
        if _contains_any(text, CANCEL_WORDS):
            self._pending_selfie = False
            self._set_state("dialog")
            self._say("好，那不拍了。")
            return
        if _contains_any(text, CONFIRM_WORDS):
            self._pending_selfie = False
            self._set_state("dialog")
            self._say("好嘞，拍拍看～")
            self._do_selfie()
            return
        # 没听清
        self._say("要拍的话说「确认」，不想拍说「不用」哦。")

    def _do_selfie(self) -> None:
        def _go():
            try:
                from . import runtime

                svc = runtime.service()
                if svc is None:
                    return
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
                    return
                # 复用完整聊天窗的程序化入口（会自动落盘、记忆、TTS 回复朗读）
                window = self._ensure_chat(app)
                if window is None:
                    self._say("对话窗口打不开，我先记下你这句。")
                    return
                window.voice_submit(text)
            except Exception:  # noqa: BLE001
                log.exception("语音消息发送到对话失败")

        QTimer.singleShot(0, _go)

    def _ensure_chat(self, app):
        """拿到（必要时打开）一个现代聊天窗，供语音投递文字。

        优先复用已打开的窗口；没有就调 app.open_modern_chat() 打开一个，
        然后取 app.modern_chat_window。
        """
        win = getattr(app, "modern_chat_window", None)
        if win is not None:
            try:
                import shiboken6

                if shiboken6.isValid(win):
                    return win
            except Exception:  # noqa: BLE001
                pass
        try:
            app.open_modern_chat()
        except Exception:  # noqa: BLE001
            log.exception("打开聊天窗失败")
        win = getattr(app, "modern_chat_window", None)
        return win

    # ------------------------------------------------------------ 出声
    def _say(self, text: str) -> None:
        """用本地 TTS 念一句（没开 TTS 就静默，只走气泡/状态）。"""
        try:
            from . import tts as tts_mod

            store = self._store()
            if store is None:
                return
            tts_mod.speak_text(text, store, kind="chat")
        except Exception:  # noqa: BLE001
            log.debug("语音助手出声失败", exc_info=True)

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
