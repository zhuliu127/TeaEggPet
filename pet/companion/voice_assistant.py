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


def _looks_hallucinated(text: str) -> bool:
    """判断一段 whisper 转写是不是「幻听」（把静音/噪声硬编出一堆字）。

    背景：把十几秒的背景噪声喂给 whisper，又被 initial_prompt 偏置到唤醒词，
    它会把提示词无限重复（日志里出现过一长串「小鱼小鱼小鱼…」）。这种结果
    绝不能当成用户说了唤醒词，否则会在没人说话时莫名其妙「被唤醒」。
    规则（命中任一即视为幻听）：
      * 去标点后长度过长（人喊唤醒词/短句不会几十上百字）；
      * 内部重复度极高（某 2~4 字片段反复出现很多次）。
    """
    t = _norm(text)
    if not t:
        return True
    n = len(t)
    if n > 24:  # 唤醒/短指令不会这么长，基本是幻听
        return True
    # 2~4 字片段重复次数检测
    for seg_len in (2, 3, 4):
        if n < seg_len * 4:
            continue
        counts = {}
        for i in range(n - seg_len + 1):
            seg = t[i:i + seg_len]
            counts[seg] = counts.get(seg, 0) + 1
        if counts and max(counts.values()) >= 4:
            return True
    return False


# ASR 幻听/字幕套话过滤词表（参考 dsh-dafeiyu-pro 实战验证：ASR 听到背景音/视频时
# 会输出字幕套话）。命中这些就当噪声丢弃，绝不能当用户说话。
_HALLUCINATION_PHRASES = (
    "谢谢观看", "感谢观看", "请订阅", "订阅", "点赞", "字幕", "出品", "独家",
    "禁止转载", "未经允许", "网易云音乐", "qq音乐", "伴奏", "纯音乐",
    "bilibili", "哔哩哔哩", "字幕由", "明镜与点点", "未经许可",
)


def _looks_like_subtitle_junk(text: str) -> bool:
    """ASR 输出像字幕套话/背景音 → 丢弃。"""
    t = _norm(text)
    if not t:
        return True
    for phrase in _HALLUCINATION_PHRASES:
        if _norm(phrase) and _norm(phrase) in t:
            return True
    return False


def _zero_crossing_rate(x) -> float:
    """过零率：单位采样内的符号翻转次数。参考实战数据，键击/碰撞/高频噪声的
    过零率显著高于人声（>0.32 基本是噪声），是最有效的噪声过滤器。"""
    import numpy as np

    a = np.asarray(x, dtype=np.float32)
    if a.shape[0] < 2:
        return 0.0
    signs = np.signbit(a)
    return float(np.count_nonzero(signs[1:] != signs[:-1])) / float(a.shape[0] - 1)


def _voiced_ratio(x, frame: int = 480) -> float:
    """有声帧占比：按 30ms 帧算 RMS，超过相对阈值（全局均值的 0.6 倍）的帧比例。
    人声通常在 0.4 以上，断续噪声低。"""
    import numpy as np

    a = np.asarray(x, dtype=np.float32)
    n = a.shape[0]
    if n < frame:
        return 0.0
    frames = n // frame
    rms = np.array([float(np.sqrt(np.mean(a[i * frame:(i + 1) * frame] ** 2)) + 1e-9)
                    for i in range(frames)])
    thr = max(1e-4, float(np.mean(rms)) * 0.6)
    return float(np.count_nonzero(rms >= thr)) / float(frames)



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
    # 跨线程发声请求：采集后台线程 emit，GUI 线程槽执行（QMediaPlayer 只能主线程用）。
    # 这比在后台线程里 QTimer.singleShot 更可靠——信号跨线程是 queued connection。
    speak_requested = Signal(str)

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
        # 「她自己在说话」时段：这段时间麦克风收到的是她自己的 TTS 声音，
        # 必须丢弃，否则会把「嗯，我在听」当成用户说话再识别一遍 → 自问自答/死循环。
        self._mute_until = 0.0
        self._busy_reply = False  # AI 正在生成/朗读回复，期间不采集新的一句
        self._busy_deadline = 0.0  # 忙碌兜底超时（防回调没来）
        self._model_main_info = ""  # 当前对话模型说明（给设置页显示）
        self._model_kws_info = ""   # 当前唤醒模型说明（给设置页显示）
        # 把发声请求接到真正的执行函数（GUI 线程里跑）
        try:
            self.speak_requested.connect(self._do_speak)
        except Exception:  # noqa: BLE001
            pass

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
        self._busy_reply = False
        self._mute_until = 0.0
        self._engine = None
        self._kws_engine = None
        self._model_main_info = ""
        self._model_kws_info = ""
        self._register_chat_hook()
        self._thread = threading.Thread(target=self._run, name="VoiceAssistant", daemon=True)
        self._thread.start()
        self._set_state("idle")
        self.status.emit("语音助手正在启动…（首次加载模型可能要等几秒）")
        return True

    # ------------------------------------------------------------ 回复联动
    def _register_chat_hook(self) -> None:
        """订阅「AI 回复完成」：清掉 _busy_reply，并把回复冒成她的气泡。

        这样用户能在桌宠身上直接看到她在回什么（豆包式一问一答的可视反馈），
        同时 AI 生成期间采集端知道她在忙、不做无谓识别。
        """
        try:
            from ..chat.service import ChatService

            ChatService.register_global_finished(self._on_chat_finished)
        except Exception:  # noqa: BLE001
            log.debug("订阅语音回复完成失败", exc_info=True)

    def _unregister_chat_hook(self) -> None:
        try:
            from ..chat.service import ChatService

            ChatService.unregister_global_finished(self._on_chat_finished)
        except Exception:  # noqa: BLE001
            pass

    def _on_chat_finished(self, user_text: str = "", reply: str = "") -> None:
        """AI 回复完成（GUI 线程回调）：解除忙碌，把回复显示成气泡。

        注意：``ChatService._finished`` 是 ``cb(finished_text)`` 这样只传一个位置参数
        （那是**回复正文**），并不是 ``(user_text, reply)``。所以要兼容两种来源：
        把「第一个非空参数」当作回复正文，避免回复被吞掉、气泡永远不显示。
        """
        self._busy_reply = False
        text = str(reply or "").strip() or str(user_text or "").strip()
        # 只有语音对话进行中才把回复冒成气泡（避免干扰手动聊天的界面）
        if self._state in ("dialog", "confirm") and text:
            try:
                from . import runtime as rt

                rt.bubble(text, 8000)
            except Exception:  # noqa: BLE001
                log.debug("显示回复气泡失败", exc_info=True)

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
            self._model_main_info = asr.model_label(asr._guess_size_from_path(__import__("pathlib").Path(model_dir))) \
                if hasattr(asr, "_guess_size_from_path") else model_dir
            log.info("语音助手主模型已加载：%s（device=%s,%s）", model_dir, device, compute)
            # 唤醒词识别模型：与对话模型**完全独立**，默认 tiny（快）。
            # 1) 用户显式填了 KWS 模型路径 → 用它
            # 2) 否则按「唤醒模型档位」companion_whisper_kws_model_size（默认 tiny）
            #    去下载目录/扫描结果里找对应 size 的模型
            # 3) 再找不到 → 退化用主模型
            kws_path = str(self._cfg("companion_whisper_kws_model_path", "") or "").strip()
            if kws_path and str(__import__("pathlib").Path(kws_path)).strip():
                self._kws_engine = asr.load_engine(kws_path, device=device, compute_type=compute)
                self._model_kws_info = kws_path
                log.info("唤醒词模型（自定义路径）：%s", kws_path)
            else:
                kws_size = str(self._cfg("companion_whisper_kws_model_size", "tiny") or "tiny")
                tiny_dir = self._find_size_dir(kws_size)
                if tiny_dir:
                    # 唤醒词模型通常用 CPU/int8 最稳（短音频，不必上 GPU）
                    self._kws_engine = asr.load_engine(tiny_dir, device="cpu", compute_type="int8")
                    self._model_kws_info = f"{kws_size}  ·  {tiny_dir}"
                    log.info("唤醒词模型 %s：%s", kws_size, tiny_dir)
                else:
                    self._kws_engine = self._engine
                    self._model_kws_info = "（未单独下载 %s，暂用对话模型）" % kws_size
                    log.info("未找到唤醒词模型 %s，唤醒词改用主模型识别", kws_size)
            return True
        except Exception as exc:  # noqa: BLE001
            log.exception("加载 Whisper 模型失败")
            self.error.emit(f"加载模型失败：{exc}")
            return False

    def _find_size_dir(self, size: str) -> str:
        """找指定 size 的模型目录（下载目录优先，其次本机扫描）；没有返回空。"""
        try:
            from pathlib import Path

            from . import asr

            download_root = str(self._cfg("companion_whisper_download_dir", "") or "").strip()
            if download_root:
                cand = Path(download_root).expanduser() / size
                if (cand / "model.bin").is_file() and (cand / "config.json").is_file():
                    return str(cand)
            for item in asr.scan_local_models([download_root] if download_root else None):
                if item["size"] == size:
                    return item["path"]
        except Exception:  # noqa: BLE001
            pass
        return ""

    def model_info(self) -> dict:
        """返回当前实际使用的模型信息（供设置页显示）。"""
        return {
            "dialog": getattr(self, "_model_main_info", "") or "",
            "wake": getattr(self, "_model_kws_info", "") or "",
        }

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
        self._unregister_chat_hook()
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
            threshold=0.45,
            min_speech_duration_ms=180,
            min_silence_duration_ms=380,
            speech_pad_ms=140,
        )
        buffer = np.zeros(0, dtype=np.float32)
        silence_blocks = 0
        speaking = False
        # 静音断句阈值（块数）：待命态（参考 §8.4 用 ~0.7s，太短会把唤醒词尾巴切掉、
        # 或"说到一半就不听了"），对话态 1.0s（给长句换气留空档）。BLOCK_MS=30ms。
        silence_limit_idle = int(0.70 / (BLOCK_MS / 1000.0))     # 待命 ~0.7s
        silence_limit_dialog = int(1.00 / (BLOCK_MS / 1000.0))   # 对话 ~1.0s
        max_seg_sec = 18.0  # 单段上限放到 18s（参考实战：12s 说长句太紧）

        # 自适应能量门限：先按一个较低初值，持续跟踪底噪（静音块 RMS 的滑窗分位）。
        # 初值压低到 0.0035 更灵敏；上限 0.015 避免嘈杂环境把门限抬过头反而压掉人声。
        energy_threshold = 0.0035
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
                rms = float(np.sqrt(np.mean(raw ** 2)) + 1e-9)

                # 她自己在说话（TTS 播报 / AI 生成回复中）：这段时间麦克风收到的是
                # 她自己的声音，直接丢弃，绝不当成用户说话，否则会自问自答死循环。
                if self._is_muted():
                    buffer = np.zeros(0, dtype=np.float32)
                    speaking = False
                    silence_blocks = 0
                    now = time.time()
                    if now - last_level_log > 3.0:
                        last_level_log = now
                        log.info("麦克风电平 rms=%.4f（她正在说话，已静音采集）状态=%s",
                                 rms, self._state)
                    continue

                # 自适应门限：把底噪推高到当前 RMS 的平滑值，门限 = 底噪 * 3 且不小于 0.003
                if rms < energy_threshold:
                    noise_floor = 0.98 * noise_floor + 0.02 * rms
                    energy_threshold = max(0.003, min(0.015, noise_floor * 3.0 + 0.002))
                speech = rms > energy_threshold

                # 每 3 秒打一条电平日志，方便排查「麦克风到底有没有信号」
                now = time.time()
                if now - last_level_log > 3.0:
                    last_level_log = now
                    log.info("麦克风电平 rms=%.4f 门限=%.4f 状态=%s", rms, energy_threshold, self._state)

                # 只有「检测到语音」才开始往缓冲里攒，避免把大量静音/噪声攒成一坨
                # 十几秒的片段丢给 whisper（whisper 会把静音硬编成一串幻听文字）。
                if not speaking and not speech:
                    # 还没开口：不攒缓冲，保持底噪跟踪即可
                    buffer = np.zeros(0, dtype=np.float32)
                    silence_blocks = 0
                    continue

                buffer = np.concatenate([buffer, chunk])

                if speech:
                    speaking = True
                    silence_blocks = 0
                else:
                    if speaking:
                        silence_blocks += 1

                seg_too_long = buffer.shape[0] > int(max_seg_sec * SAMPLE_RATE)
                # 静音断句阈值随状态变化（参考 dsh-dafeiyu-pro 实战参数）：
                # 对话态必须长一点（~1.0s），否则长句换气就被切断、"说到一半不听了"；
                # 待命态短一点，唤醒词说完迅速出结果，响应更快。
                if self._state == "idle":
                    cur_silence_limit = silence_limit_idle
                else:
                    cur_silence_limit = silence_limit_dialog
                seg_done = speaking and silence_blocks >= cur_silence_limit
                if not (seg_done or seg_too_long):
                    continue
                # 一段话结束：取出有语音的部分
                audio = buffer.copy()
                buffer = np.zeros(0, dtype=np.float32)
                speaking = False
                silence_blocks = 0
                if audio.shape[0] < int(0.3 * SAMPLE_RATE):
                    continue
                # Silero VAD 精修（去掉首尾/中间静音）；去掉后没有语音就整段丢弃
                try:
                    ts = get_speech_timestamps(audio, vad_options=vad_opts)
                    if ts:
                        a = ts[0]["start"]; b = ts[-1]["end"]
                        audio = audio[a:b]
                    else:
                        # VAD 认为整段都没有语音 → 是噪声/静音，直接丢弃，不喂 whisper
                        continue
                except Exception:  # noqa: BLE001
                    pass
                if audio.shape[0] < int(0.25 * SAMPLE_RATE):
                    continue
                # 再按实际能量复核一遍：有效语音占比太低也丢掉（防噪声误触发）
                try:
                    seg_rms = float(np.sqrt(np.mean(audio ** 2)) + 1e-9)
                    if seg_rms < 0.003:
                        continue
                    # 过零率门控：键击/碰撞/高频噪声过零率明显高于人声（实战 >0.32 基本是噪声）。
                    # 这是参考 dsh-dafeiyu-pro 验证过的最有效噪声过滤器（占噪声过滤 70%）。
                    zcr = _zero_crossing_rate(audio)
                    if zcr > 0.35:
                        log.info("片段过零率过高(%.2f)，判为噪声丢弃", zcr)
                        continue
                    # 有声帧占比门控：人声通常 >0.4，断续噪声偏低
                    voiced = _voiced_ratio(audio)
                    if voiced < 0.30:
                        log.info("片段有声帧占比过低(%.2f)，判为噪声丢弃", voiced)
                        continue
                except Exception:  # noqa: BLE001
                    pass
                # 待命态「唤醒词长度窗口」（参考 dsh-dafeiyu-pro §8.6）：唤醒词典型 0.8~2s，
                # 超过 ~3.5s 基本是旁人闲聊/电视，不是叫名字 → 丢弃不送识别。
                # 既省算力、又避免长句被硬编成唤醒词造成误唤醒。
                if self._state == "idle":
                    seg_sec = audio.shape[0] / float(SAMPLE_RATE)
                    if seg_sec > 3.5:
                        log.info("待命片段过长(%.1fs)，超出唤醒词窗口，丢弃", seg_sec)
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
            # 只做唤醒词匹配（用 KWS 小模型，短促）。用 initial_prompt 把 tiny
            # 往「唤醒词」上偏置，短词识别更准。
            wake = self._wake_word()
            text = asr.transcribe(self._kws_engine, audio, language="zh",
                                  vad_filter=False, beam_size=1,
                                  initial_prompt=f"{wake}。")
            self.heard.emit(f"[待命] {text}")
            log.info("待命听到：%r（唤醒词=%r）", text, wake)
            # 幻听过滤：把静音/噪声硬编成一长串重复字的，绝不当成唤醒
            if _looks_hallucinated(text):
                log.info("待命片段疑似幻听，忽略：%r", text)
                return
            if _looks_like_subtitle_junk(text):
                log.info("待命片段疑似字幕套话，忽略：%r", text)
                return
            if _contains_any(text, [wake]) or self._fuzzy_wake(text):
                self._enter_dialog()
            return

        # 对话 / 确认模式：完整转写
        self._last_active = time.time()
        # 采集循环里已经做过能量+过零率+有声帧占比三重门控、也用 Silero 精修过，
        # 这里**不再用 whisper 内部 vad_filter**——它偶发把整段话判成「无语音活动」
        # 而整段返回空（日志「VAD filter removed 00:00.000」后无任何 heard），
        # 导致用户说了话却「没有任何回应」。关掉它，交给前面已过滤好的音频。
        text = asr.transcribe(self._engine, audio, language="zh",
                              vad_filter=False, beam_size=int(self._cfg("companion_whisper_beam", 1) or 1))
        self.heard.emit(text)
        if not text.strip():
            return
        # 字幕套话/背景音幻听：别当成用户说的话（会污染对话）
        if _looks_like_subtitle_junk(text):
            log.info("对话片段疑似字幕套话，忽略：%r", text)
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
        """唤醒词模糊匹配：模型把「小鱼小鱼」识别成「小鱼小玉/小雨小雨」之类也认。

        规则（宁缺毋滥，怕误唤醒）：
        * 逐字按顺序命中比例要够；且
        * 首字必须对得上（否则「今天天气不错」这种完全不相关的不能过）。
        """
        wake = _norm(self._wake_word())
        norm = _norm(text)
        if not wake or not norm:
            return False
        # 逐字按顺序匹配
        j = 0
        hit = 0
        for ch in norm:
            if j < len(wake) and ch == wake[j]:
                hit += 1
                j += 1
        # 字符计数匹配（容忍同音错字，如「鱼」听成「雨/玉」；「小鱼小鱼」里两个「小」
        # 都要对上）。命中的 wake 字符数 / wake 长度。
        from collections import Counter

        wc = Counter(wake)
        nc = Counter(norm)
        overlap = sum(min(cnt, nc[ch]) for ch, cnt in wc.items())
        first_ok = wake[0] in norm
        # 至少命中一半以上且首字在，才认（宁可让她没被唤醒也不要被随便一句话唤醒）
        if first_ok and overlap >= max(2, (len(wake) + 1) // 2):
            return True
        if hit >= max(2, len(wake) - 1):
            return True
        # 兜底：去掉首个重复字后（「小鱼」）整段出现
        base = wake.replace(wake[0], "", 1) if len(wake) > 1 else wake
        return bool(base and _norm(base) in norm)

    def _enter_dialog(self) -> None:
        self._last_active = time.time()
        self._set_state("dialog")
        self.woke.emit()
        self._greet()
        self.status.emit("已唤醒，正在对话中（说「%s」结束）" % self._sleep_word())

    def _greet(self) -> None:
        """被唤醒的正反馈：说一段打招呼的话 + 让桌宠做一个动画动作，并清空麦克风缓冲。

        关键是「先给她自己说话留够静音窗口」（_say 会设 _mute_until），把
        打招呼这段话的回声挡在采集之外，招呼说完后才真正开始听用户说什么。
        """
        import random

        # 先清掉唤醒前残留的音频，避免把唤醒词那句拖进这一轮
        self._last_active = time.time()
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
        # 已在等上一条回复时，不重复提交（但麦克风仍继续听，用户可以说完再等）。
        # _is_muted() 会在 _busy_deadline 到点时自愈清掉这个标志，这里再判一次。
        if self._busy_reply and time.time() < self._busy_deadline:
            log.info("上一条回复尚未完成，跳过这条的重复提交：%r", text)
            return
        # 从这刻起她在「忙」：只用于「防重复提交同一句」，**不再用于静音麦克风**
        # （静音只由 TTS 实际播放窗口决定，见 _is_muted）。
        self._busy_reply = True
        self._busy_deadline = time.time() + 10.0
        self._last_active = time.time()

        def _go():
            try:
                from . import runtime

                app = runtime.app()
                if app is None:
                    self._busy_reply = False
                    return
                # 复用完整聊天窗的程序化入口（会自动落盘、记忆、TTS 回复朗读）
                window = self._ensure_chat(app)
                if window is None:
                    self._busy_reply = False
                    self._say("对话窗口打不开，我先记下你这句。")
                    return
                window.voice_submit(text)
            except Exception:  # noqa: BLE001
                self._busy_reply = False
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
        """用本地 TTS 念一句（没开 TTS 就静默），同时在桌宠上冒一个气泡，让反馈看得见。

        **务必在 GUI 线程里出声**：``_say`` 通常由采集后台线程（``_run`` →
        ``_handle_segment`` / ``_greet``）调用，而本地 TTS 的播放用的是
        ``QMediaPlayer`` / ``QAudioOutput``——它们只能在 Qt 主线程使用。之前直接在
        后台线程里调 ``tts.speak_text``，合成可能会跑，但 ``QMediaPlayer.play()``
        在非主线程上是静默失败的（用户「只有气泡没有声音」），而且跨线程乱建 Qt
        对象还会把事件循环搞坏，导致点一下桌宠就「未响应」卡死。
        这里用 ``QTimer.singleShot(0, ...)`` 把真正的发声投递回 GUI 线程，
        气泡与静音窗口的估算也一并放进去，保证线程安全。
        """
        text = str(text or "").strip()
        if not text:
            return

        # 从任何线程安全投递到 GUI 线程：信号是 queued connection，比跨线程 QTimer 稳。
        try:
            self.speak_requested.emit(text)
        except Exception:  # noqa: BLE001
            log.debug("语音助手无法投递发声请求，直接执行", exc_info=True)
            self._do_speak(text)

    def _do_speak(self, text: str) -> None:
        """真正发声（必须在 GUI 线程执行）：冒气泡 + 本地 TTS + 估算静音窗口。"""
        if not text:
            return
        # 气泡：无论有没有开 TTS 都能看到的一句话反馈
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
                # 念这句大概要多久：按字数估（中文约 5 字/秒），至少 1.2 秒静默防抖，
                # 防止把她自己的声音当成用户说话。
                # **务必封顶**：曾经因为一段转写异常长（幻听）导致 dur 被算成几十秒，
                # 静音窗口把麦克风堵死一大段时间，用户说什么都听不到。这里把估算
                # 字符数和总时长都限制在合理范围（一句话不会超过 8 秒）。
                dur = max(1.2, min(8.0, len(text) / 5.0 + 0.6))
                self._mute_until = max(self._mute_until, time.time() + dur)
        except Exception:  # noqa: BLE001
            log.debug("语音助手出声失败", exc_info=True)

    def _tts_busy(self) -> bool:
        """本地 TTS 是否正在出声（用于采集端丢弃自己的回声）。"""
        try:
            from . import tts as tts_mod

            return bool(getattr(tts_mod.speaker(), "busy", False))
        except Exception:  # noqa: BLE001
            return False

    def _is_muted(self) -> bool:
        """是否需要丢弃当前麦克风数据（避免把「她自己说话」当成用户语音）。

        **务必保守**：只有下面几种「我们确知她在出声」的短暂时段才静音，
        任何时刻都不能永久静音，否则麦克风收不到用户声音 → 永远唤不醒。

        之前出过一次严重事故：直接读全局 TTS 的 ``busy`` 判断，而那个标志一旦
        因为合成线程异常/本地 TTS 服务断开而没复位，就会**永久 True**，采集端
        把每一帧都丢掉（日志里整屏都是「她正在说话，已静音采集」），于是用户
        对着麦克风喊什么都没用。现在改成：
        1) ``_mute_until``：只由我们自己 ``_say``/念回复时设置，按字数估算 + 兜底上限；
        2) ``_busy_reply``：AI 回复生成期间，带 25 秒硬超时；
        3) 全局 TTS busy：**只在上面两个窗口之内**才采信，避免被外部卡死的 busy 拖死。
        """
        now = time.time()
        # 回复忙碌硬超时：回调没来也不超过 10 秒（原 25s 太长，AI 若生成失败/慢，
        # 麦克风会被堵死一整段，用户说什么都「没回应」，连「休息一下」也叫不动。
        # 现在只当「别把同一句重复提交」用，且很快自愈。）
        if self._busy_reply and self._busy_deadline and now > self._busy_deadline:
            self._busy_reply = False
        # 静音窗口兜底上限（防止 _mute_until 因异常被设得离谱）
        if self._mute_until and now > self._mute_until + 30.0:
            self._mute_until = 0.0

        in_own_window = now < self._mute_until
        # **只在我们确知她真的在出声（我们自己设定的 _mute_until 静音窗口）时静音麦克风。**
        # 绝不再采信全局 TTS 的 ``busy``——那是外部状态，本地 TTS 服务断开/合成线程
        # 卡死时 busy 会**永久 True**，采集端就会整屏「（她正在说话，已静音采集）」，
        # 用户说什么都没反应（实测日志：唤醒后 22 秒连续静音采集，rms 全是 0.0000）。
        # _busy_reply 也只用于防重复提交，不参与静音判断（等回复期间麦克风照听）。
        # _mute_until 由 _do_speak 按字数估算且封顶 8 秒，且有 +30s 兜底清零，绝不会永久静音。
        return bool(in_own_window)


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
