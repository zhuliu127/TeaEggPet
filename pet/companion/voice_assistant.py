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
        """启动监听。返回 True 表示已在跑。失败时不抛，只发 error 信号。"""
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
        # 主模型
        from . import asr

        model_dir = asr.resolve_model_dir(self._cfg)
        if not model_dir:
            self.error.emit(
                "还没有可用的 Whisper 模型：请到「陪伴扩展设置 → 语音助手」里"
                "选一个模型点「下载」，或填入已有模型目录。")
            return False
        try:
            main_size = str(self._cfg("companion_whisper_model_size", "base") or "base")
            device = str(self._cfg("companion_whisper_device", "cpu") or "cpu")
            compute = str(self._cfg("companion_whisper_compute", "int8") or "int8")
            self._engine = asr.load_engine(model_dir, device=device, compute_type=compute)
            # 唤醒词识别模型：默认 tiny（快）；若用户设了就用它
            kws_path = str(self._cfg("companion_whisper_kws_model_path", "") or "").strip()
            if kws_path:
                self._kws_engine = asr.load_engine(kws_path, device=device, compute_type=compute)
            else:
                # 用同一个主引擎也可（只是慢一点）；优先尝试 tiny
                tiny_dir = self._find_tiny(model_dir)
                self._kws_engine = asr.load_engine(tiny_dir, device=device, compute_type=compute) if tiny_dir else self._engine
        except Exception as exc:  # noqa: BLE001
            log.exception("加载 Whisper 模型失败")
            self.error.emit(f"加载模型失败：{exc}")
            return False

        self._device_index = self._resolve_device()
        self._stop.clear()
        self._pending_selfie = False
        self._last_active = time.time()
        self._thread = threading.Thread(target=self._run, name="VoiceAssistant", daemon=True)
        self._thread.start()
        self._set_state("idle")
        self.status.emit("语音助手已开启，说「%s」叫醒我～" % self._wake_word())
        return True

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

    def _resolve_device(self) -> int | None:
        """按配置的麦克风名找设备序号；空/找不到走系统默认。"""
        name = str(self._cfg("companion_voice_device", "") or "").strip()
        if not name:
            return None
        try:
            import sounddevice as sd

            for idx, dev in enumerate(sd.query_devices()):
                if dev.get("max_input_channels", 0) > 0 and name in str(dev.get("name", "")):
                    return idx
        except Exception:  # noqa: BLE001
            pass
        return None

    def stop(self) -> None:
        self._stop.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=3.0)
        self._thread = None
        self._stream = None
        self._set_state("off")

    def reload(self) -> None:
        """配置改了之后重来一遍（关着就不动）。"""
        if self._enabled():
            self.stop()
            QTimer.singleShot(300, self.start)
        else:
            self.stop()

    # ------------------------------------------------------------ 主循环
    def _run(self) -> None:
        import numpy as np
        import sounddevice as sd
        from faster_whisper.vad import get_speech_timestamps, VadOptions

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
        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                blocksize=BLOCK, device=self._device_index,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001
            log.exception("打开麦克风失败")
            self.error.emit(f"打不开麦克风：{exc}")
            self._set_state("off")
            return

        try:
            while not self._stop.is_set():
                try:
                    data, overflowed = self._stream.read(BLOCK)
                except Exception:  # noqa: BLE001
                    break
                chunk = data[:, 0].copy() if data.ndim > 1 else data.copy()
                buffer = np.concatenate([buffer, chunk])
                # 用简单能量门限累积「可能是话」的段，再用 VAD 精确切
                rms = float(np.sqrt(np.mean(chunk ** 2)) + 1e-9)
                speech = rms > 0.006        # 能量门限（挡掉底噪）
                if speech:
                    speaking = True
                    silence_blocks = 0
                else:
                    if speaking:
                        silence_blocks += 1
                seg_limit = int(max_seg_sec / (BLOCK_MS / 1000.0))
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
        self._say("嗯，我在听～")
        self.status.emit("已唤醒，正在对话中（说「%s」结束）" % self._sleep_word())

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
