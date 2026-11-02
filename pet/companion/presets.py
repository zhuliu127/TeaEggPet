# -*- coding: utf-8 -*-
"""关键词库（情境 / 神态 / 动作 / 镜头 / 光线）。

模型只填关键词名字，这里展开成具体画面描写——姿势表情取景由文字决定，
参考图才不会被逐张照抄（与 dsh-image-studio 的 pursets 同源同格式）。
"""

from __future__ import annotations

from typing import Any

PRESET_GROUPS = ("situation", "expression", "pose", "shot", "light")
PRESET_LABEL = {
    "situation": "情境",
    "expression": "神态",
    "pose": "动作",
    "shot": "镜头",
    "light": "光线",
}


def _p(name, aliases, prompt, **extra):
    item = {"name": name, "aliases": list(aliases), "prompt": prompt}
    item.update(extra)
    return item


DEFAULT_PRESETS: dict[str, list[dict[str, Any]]] = {
    "situation": [
        {"name": "在家休息", "aliases": ["在家", "宅家", "休息", "放松", "居家"], "scene": "卧室", "outfit": "居家针织服",
         "light": "午后暖阳", "prompt": "放松慵懒的居家氛围，周围有生活感的小物件"},
        {"name": "睡前", "aliases": ["睡觉", "晚安", "熬夜"], "scene": "卧室", "outfit": "居家针织服",
         "light": "夜晚台灯", "prompt": "夜深了，准备睡觉，被子和枕头有些凌乱，安静温馨"},
        {"name": "刚睡醒", "aliases": ["起床", "早安", "赖床"], "scene": "卧室", "outfit": "居家针织服",
         "light": "清晨柔光", "prompt": "刚睡醒，头发有点乱，还带着困意"},
        {"name": "客厅放松", "aliases": ["看电视", "沙发", "窝沙发"], "scene": "客厅", "outfit": "居家针织服",
         "light": "午后暖阳", "prompt": "窝在沙发上，悠闲自在"},
        {"name": "工作学习", "aliases": ["上班", "工作", "学习", "干活", "写代码"], "scene": "", "outfit": "通勤休闲装",
         "light": "", "prompt": "在电脑或书桌前专注做事，桌上有电脑、笔记和一杯饮料"},
        {"name": "外出", "aliases": ["出门", "逛街", "约会", "散步", "溜达"], "scene": "", "outfit": "复古",
         "light": "午后暖阳", "prompt": "在户外，轻松愉快，背景是街道或公园"},
        {"name": "吃东西", "aliases": ["吃饭", "下午茶", "零食"], "scene": "", "outfit": "", "light": "",
         "prompt": "面前摆着诱人的食物，正准备开动或吃得很满足"},
        {"name": "下雨天", "aliases": ["下雨", "雨天"], "scene": "", "outfit": "", "light": "阴雨天",
         "prompt": "窗外在下雨，玻璃上有雨痕，氛围安静"},
    ],
    "expression": [
        _p("开心", ["高兴", "愉快", "笑"], "眼睛弯成月牙，露出灿烂的笑容，神情明亮有活力"),
        _p("微笑", ["温柔"], "嘴角轻轻上扬的温柔微笑，眼神柔和"),
        _p("害羞", ["脸红", "不好意思"], "脸颊泛起红晕，眼神躲闪、微微低头，嘴角含着不好意思的笑"),
        _p("得意", ["骄傲", "神气"], "扬起下巴，眯着眼露出得意的坏笑，一副「快夸我」的神气"),
        _p("撒娇", ["卖萌"], "歪着头，眼睛亮晶晶地往上看，嘴角带着讨好的笑"),
        _p("委屈", ["可怜"], "眉头微蹙，嘴巴瘪起，眼里含着要掉不掉的泪光"),
        _p("生气", ["气鼓鼓"], "鼓起腮帮子，眉毛竖起，气呼呼地瞪着前方"),
        _p("惊讶", ["吃惊"], "眼睛睁大，嘴巴微张成 O 形，眉毛高高扬起"),
        _p("困倦", ["困", "犯困", "没睡醒"], "半睁着惺忪的睡眼，打着哈欠或眼皮打架，神情迷糊"),
        _p("认真", ["专注"], "神情专注，目光坚定，嘴唇轻抿"),
        _p("平静", ["放空", "发呆"], "表情放松平和，目光自然或有点出神"),
        _p("难过", ["伤心"], "眼神低垂黯淡，嘴角下撇，带着淡淡的忧伤"),
        _p("无语", ["无奈"], "半眯着眼，嘴角抽动，一脸「真拿你没办法」的无奈"),
        _p("紧张", ["慌张"], "额角冒出细汗，眼神慌乱，嘴唇紧抿"),
    ],
    "pose": [
        _p("站着", ["站立"], "自然地站着，重心落在一条腿上，身体略微侧转"),
        _p("坐着", ["坐"], "自然地坐着，身体放松，双手放在膝上或身侧"),
        _p("抱膝坐", ["抱膝"], "坐在地上或床上，双臂环抱膝盖，下巴搁在膝头"),
        _p("躺着", ["躺"], "侧身或仰面躺着，身体舒展放松"),
        _p("趴着", ["趴"], "趴在床上或桌上，双手托着下巴，小腿向上翘起"),
        _p("伸懒腰", [], "双臂高举过头伸懒腰，身体舒展"),
        _p("托腮", ["撑脸"], "一只手托着脸颊，手肘撑在桌面上"),
        _p("挥手", ["打招呼"], "抬起一只手朝镜头挥手打招呼"),
        _p("回头", ["回眸"], "背对镜头，转过头回眸看向镜头"),
        _p("比耶", ["剪刀手"], "一只手在脸旁比出 V 字手势"),
        _p("抱着东西", ["抱着"], "双手把东西抱在怀里"),
        _p("看书", ["读书"], "低着头专注地看着手里的书"),
        _p("喝饮料", ["喝咖啡", "捧着杯子"], "双手捧着杯子，小口喝着热饮"),
        _p("奔跑", ["跑"], "向前小跑，头发和衣摆随动作飘起"),
    ],
    "shot": [
        _p("自拍", ["自拍照"], "手机自拍视角：人物一只手臂伸向镜头举着手机（手机在画面外），轻微俯拍，脸离镜头较近"),
        _p("特写", ["大头"], "面部特写，聚焦表情和眼神"),
        _p("近景", ["胸像"], "胸口以上的近景，人物占画面大部分"),
        _p("半身", [], "腰部以上的半身构图"),
        _p("全身", [], "全身入镜，能看清完整的姿势和服装"),
        _p("远景", ["大场景"], "人物在画面中较小，突出环境与氛围"),
        _p("俯视", ["俯拍"], "从高处向下俯拍的视角"),
        _p("仰视", ["仰拍"], "从低处向上仰拍的视角"),
        _p("第一人称", ["POV", "主观视角"], "第一人称视角，观看者就在人物面前，人物与镜头有互动"),
    ],
    "light": [
        _p("午后暖阳", ["午后", "阳光"], "午后温暖的阳光从窗户斜照进来，有柔和的光斑，整体暖色调"),
        _p("清晨柔光", ["清晨", "早上"], "清晨柔和的自然光，色调清新明亮"),
        _p("黄昏逆光", ["黄昏", "夕阳"], "黄昏的橙色逆光，人物轮廓镶着一圈金边"),
        _p("夜晚台灯", ["夜晚", "晚上", "台灯"], "夜晚的室内，只有暖黄的台灯照明，周围偏暗，温馨安静"),
        _p("霓虹夜景", ["霓虹", "夜景"], "夜晚街头的霓虹灯光，蓝紫与粉色的光影交错"),
        _p("阴雨天", ["阴天", "雨天光"], "阴天或下雨，光线柔和偏冷，氛围安静"),
        _p("月光", [], "夜晚冷蓝色的月光，静谧"),
    ],
}


def _norm(value: Any) -> str:
    return "".join(str(value or "").strip().lower().split())


def _clean_item(group: str, raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:40]
    if not name:
        return None
    aliases = raw.get("aliases")
    if isinstance(aliases, str):
        aliases = [x for x in aliases.replace("，", ",").replace("、", ",").split(",")]
    aliases = [str(x).strip()[:40] for x in (aliases or []) if str(x).strip()][:12]
    item: dict[str, Any] = {"name": name, "aliases": aliases, "prompt": str(raw.get("prompt") or "").strip()[:600]}
    if group == "situation":
        for key in ("scene", "outfit", "light"):
            item[key] = str(raw.get(key) or "").strip()[:80]
    return item


def normalize(raw: Any) -> dict[str, list[dict[str, Any]]]:
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, list[dict[str, Any]]] = {}
    for group in PRESET_GROUPS:
        items = raw.get(group)
        if not isinstance(items, list) or not items:
            items = DEFAULT_PRESETS[group]
        cleaned = []
        seen = set()
        for entry in items:
            item = _clean_item(group, entry)
            if not item or item["name"] in seen:
                continue
            seen.add(item["name"])
            cleaned.append(item)
        out[group] = cleaned[:80]
    return out


def load(store) -> dict[str, list[dict[str, Any]]]:
    raw = store.read_json(store.presets_path(), None)
    if raw is None:
        data = normalize(DEFAULT_PRESETS)
        try:
            store.write_json(store.presets_path(), data)
        except Exception:
            pass
        return data
    return normalize(raw)


def save(store, data) -> dict[str, list[dict[str, Any]]]:
    cleaned = normalize(data)
    store.write_json(store.presets_path(), cleaned)
    return cleaned


def find_preset(group: str, ref: Any, presets: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    key = _norm(ref)
    if not key:
        return None
    items = presets.get(group) or []
    for item in items:
        if _norm(item.get("name")) == key:
            return item
    for item in items:
        if any(_norm(a) == key for a in item.get("aliases") or []):
            return item
    loose = [item for item in items
             if any(len(_norm(n)) >= 2 and (_norm(n) in key or key in _norm(n))
                    for n in [item.get("name")] + list(item.get("aliases") or []))]
    return loose[0] if len(loose) == 1 else None


def find_situation(ref: Any, presets) -> dict[str, Any] | None:
    return find_preset("situation", ref, presets)


def keyword_text(presets) -> str:
    lines = []
    for group in PRESET_GROUPS:
        items = presets.get(group) or []
        if not items:
            continue
        lines.append(f"{PRESET_LABEL[group]}（{group}）：" + "、".join(i["name"] for i in items))
    return "\n".join(lines)


def reset_group(store, group: str) -> dict[str, list[dict[str, Any]]]:
    """把某一组关键词恢复成内置默认（其它组不动）。"""
    if group not in PRESET_GROUPS:
        raise ValueError(f"未知的关键词分组：{group}")
    data = load(store)
    data[group] = [dict(item) for item in DEFAULT_PRESETS[group]]
    return save(store, data)
