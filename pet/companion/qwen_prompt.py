# -*- coding: utf-8 -*-
"""把「她此刻在做什么」翻译成 Qwen-Image 2.1 编辑模型爱吃的指令。

为什么需要这一层：Qwen-Edit 训练用的是「<image1> 是人物、<image2> 是场景、把 A 换成 B…」
这种**单段、直接、点名每张图职责**的编辑指令。我们原来喂给它的是一大段带【画面】
【情境】【参考图说明】的中文说明书 + 一堆免责话术，模型只会照着参考图糊一遍。

这里紧贴用户给的《Edit Prompt Enhancer》模板：
- 系统提示 = 模板原文（可被配置里的路径覆盖）
- 用户消息 = 每张参考图的编号与职责 + 此刻日程 + 镜头/姿势/表情/光线 + 画幅
- 输出 = {rewritten_prompt, wh_ratio, ratio_follow}
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from . import llm

log = logging.getLogger(__name__)

TEMPLATE_FILE = Path(__file__).with_name("prompts") / "qwen_edit_enhancer.txt"

#: 每类参考图「该取什么」，写进用户消息里，模型不用猜
ROLE_TEXT = {
    "characters": "人物参考：**同时是整张图的画风基准**——长相（脸型五官、眼睛、发色发型、体型、"
                  "鱼鳍耳/鲸鱼尾巴）和线条、上色、光影质感、画面完成度都以它为准；"
                  "她身上原来的衣服不要取",
    "outfits": "服饰参考：只取这套衣服的款式、颜色、材质与配饰，图中展示的那个人（脸、发型、身体）不要取",
    "scenes": "场景参考：取环境的空间布局、家具陈设、配色与光线；图里的人不要取",
    "props": "道具参考：只取外形、材质与配色",
}
CATEGORY_LABEL = {
    "characters": "人物", "outfits": "服饰", "scenes": "场景", "props": "道具",
}

#: 输出风格（硬要求）。本地模型不看这句话就会往「半写实 / 3D」跑，所以既写进请求，
#: 也在返回后做一次兜底校验。可在设置里改（companion_qwen_style）。
DEFAULT_STYLE = (
    "画风严格对齐 <image1>：日系精美插画风格，柔和暖光、逆光发丝、光斑，极其丰富的细节；"
    "线条、上色、皮肤与发丝的光影质感都与它一致，画面中不添加任何文字或界面元素"
)
#: 判断「模型有没有写风格」的关键词
STYLE_MARKERS = ("二次元", "动漫", "插画", "anime", "2d", "赛璐璐", "日系")


def _candidates(store) -> list[Path]:
    out: list[Path] = []
    custom = str(store.get("companion_qwen_prompt_template", "") or "").strip()
    if custom:
        out.append(Path(custom))
    out.append(TEMPLATE_FILE)
    # 打包后 __file__ 指向 _internal/pet/companion/qwen_prompt.py（PYZ 里的假路径），
    # 同级 prompts/ 由 --add-data 放进去；再兜一层 _MEIPASS，换 onedir/onefile 都不怕。
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        out.append(Path(meipass) / "pet" / "companion" / "prompts" / "qwen_edit_enhancer.txt")
    out.append(Path(__file__).resolve().parent / "prompts" / "qwen_edit_enhancer.txt")
    return out


def template_text(store) -> str:
    for path in _candidates(store):
        try:
            text = path.read_text(encoding="utf-8")
            if text.strip():
                return text
        except OSError:
            continue
    log.warning("读不到提示词模板（试过 %s）", [str(p) for p in _candidates(store)])
    return ""


def _describe_refs(refs: list[dict[str, Any]]) -> str:
    lines = []
    for index, ref in enumerate(refs, start=1):
        category = str(ref.get("category") or "")
        name = str(ref.get("entry") or "")
        role = ROLE_TEXT.get(category, "参考图")
        line = f"<image{index}>：{role}（这里给的是「{name}」）"
        # 关键：把库里的外观要点写进去。只给编号不写长相，模型就会把人物画成本地模型的
        # 通用脸（实测头发从深蓝变棕、鱼鳍耳和尾巴都没了）。
        brief = _brief((ref.get("entry_data") or {}).get("description"), 110 if category == "characters" else 70)
        if brief:
            label = "长相要点" if category == "characters" else ("服装要点" if category == "outfits" else "内容要点")
            line += f"，{label}：{brief}"
        traits = _brief((ref.get("entry_data") or {}).get("traits"), 60) if category == "characters" else ""
        if traits:
            line += f"；神态特点：{traits}"
        lines.append(line)
    return "\n".join(lines)


def _brief(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if not value:
        return ""
    return value[:limit] + ("…" if len(value) > limit else "")


SELFIE_NO_ARM = (
    "这是一张她自己发给我的自拍：她把手机举在画面之外、人凑近镜头。\n"
    "   · 近景半身：头到腰（或盘着的腿）在画面里，人物占画面 70% 以上；\n"
    "   · 头顶贴近画面上缘，脸在画面中上部，五官完整清晰、不被裁切；\n"
    "   · 画面里**完全不出现**举手机的那只手臂——不画前臂、不画手、也不要在角落里"
    "塞一段胳膊或袖口；就是她对着镜头，镜头外的事看不见。\n"
    "   · 她自己的两只手照常出现在该在的地方（抱着抱枕、放在腿上、握手柄都行）。"
)

SELFIE_WITH_ARM = (
    "这是一张她自己发给我的自拍：一只手举着手机伸向镜头（手机在画面外或只露一角）。\n"
    "   · 近景半身：人物占画面 70% 以上，头顶贴近画面上缘，脸在画面中上部；\n"
    "   · 伸向镜头的那只手臂只从画面一个角落斜切进来，长度不超过画面高度的四分之一，"
    "绝对不能成一条从画面边缘一直连到身体的长胳膊。"
)


def build_user_message(*, refs: list[dict[str, Any]], item: dict[str, Any], plan: dict[str, Any],
                       aspect: str, weekday: str = "", store_style: str = "",
                       arm_visible: bool = False) -> str:
    """拼「这次要生成什么」。只描述事实，判断交给模板里的模型。"""
    bits: list[str] = []
    if refs:
        bits.append("参考图（按顺序，编号必须写成 <imageN>）：\n" + _describe_refs(refs))
    else:
        bits.append("参考图：没有（纯文生图）")

    when = []
    if weekday:
        when.append(weekday)
    if item:
        span = f"{item.get('start', '')}–{item.get('end', '')}".strip("–")
        if span:
            when.append(span)
        act = str(item.get("activity") or "")
        if act:
            when.append(act)
        where = str(item.get("scene") or item.get("location") or "")
        if where:
            when.append(f"在{where}")
    detail = str((item or {}).get("detail") or "")
    scene_line = "、".join(when)
    if detail:
        scene_line += f"（{detail}）"

    story = [
        f"她此刻的生活：{scene_line}" if scene_line else "她就在家里随手拍一张。",
        f"这张照片的内容：{plan.get('prompt') or '她举起手机自拍一张'}",
    ]
    camera = []
    for key, label in (("shot", "取景"), ("pose", "姿势"), ("expression", "表情"), ("light", "光线")):
        value = str(plan.get(key) or "").strip()
        if value:
            camera.append(f"{label}：{value}")
    if camera:
        story.append("；".join(camera))
    story.append(SELFIE_WITH_ARM if arm_visible else SELFIE_NO_ARM)
    if aspect:
        story.append(f"目标画幅：{aspect}（竖版优先）。")
    bits.append("这次要生成：\n" + "\n".join(f"- {x}" for x in story))

    style = str(store_style or DEFAULT_STYLE).strip()
    bits.append(
        "写 rewritten_prompt 时的硬要求：\n"
        "1. 每张参考图用 <imageN> 点名职责，写清「取它的什么、不要它的什么」。\n"
        "1.15 构图＝近景半身自拍：人物占画面 70% 以上、镜头离她近；头顶靠近画面上缘，脸在中上部。"
        "画面里不出现伸向镜头的手臂（前臂、手、袖口都不要出现在画面前景），"
        "严禁超长手臂或把整条胳膊拉进画面。\n"
        "1.16 镜头距离与人物大小参考 <image1>（它本身就是近景自拍），不要把她画远。\n"
        "1.1 画风只跟 <image1>：它既是人物长相的唯一来源，也是线条/上色/光影/完成度的基准；"
        "其余参考图只用来**替换**服装、场景、道具，不要把它们的画风、模特、构图带进来。\n"
        "2. 服饰要拆成具体单品逐个写出来（例如「白色水手领衬衫、红色领结、米色粗针织开衫、"
        "藏青色百褶裙」），不要只写「穿 X 套装」。\n"
        "3. 结尾必须带上这段风格约束（可以润色措辞，但不能缺席）：" + style + "。\n"
        "4. 只输出那个 JSON，不要解释。"
    )
    bits.append("按上面的规则，只输出那个 JSON。")
    return "\n\n".join(bits)


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").replace("\r", " ").replace("\n", " ").split())


def _style_present(prompt: str, style: str) -> bool:
    """判断 rewritten_prompt 里是不是已经带上风格约束。

    不能只认「二次元 / 插画」这类固定词：用户可以把画风改成任意措辞
    （例如「画风严格对齐 <image1>：线条、上色……」），那样固定词永远匹配不上，
    就会每张图都白追加一次。所以先拿风格串自己的片段做指纹比对。
    """
    flat_prompt = "".join(str(prompt or "").split())
    flat_style = "".join(str(style or "").split()).replace("；", "，")
    for chunk in flat_style.split("，"):
        probe = chunk.strip("。;:： ")
        if len(probe) >= 10 and probe in flat_prompt:
            return True
    return any(mark in str(prompt or "").lower() for mark in STYLE_MARKERS)


def enhance(store, *, refs: list[dict[str, Any]], item: dict[str, Any], plan: dict[str, Any],
            aspect: str, weekday: str = "", arm_visible: bool = False) -> dict[str, Any]:
    """问一次模型，拿 {rewritten_prompt, wh_ratio, ratio_follow}。失败抛异常给调用方兜底。"""
    system = template_text(store)
    if not system:
        raise llm.CompanionLLMError("提示词模板为空")
    style = str(store.get("companion_qwen_style", "") or "").strip() or DEFAULT_STYLE
    message = build_user_message(refs=refs, item=item, plan=plan, aspect=aspect, weekday=weekday,
                                 store_style=style, arm_visible=arm_visible)
    text = llm.ask(
        store.cfg,
        [{"role": "system", "content": system}, {"role": "user", "content": message}],
        max_tokens=1600,
        temperature=0.6,
        timeout=120,
        json_mode=True,
    )
    data = llm.extract_json(text)
    if not isinstance(data, dict):
        raise llm.CompanionLLMError("提示词增强没有返回 JSON 对象")
    prompt = _one_line(data.get("rewritten_prompt"))
    if not prompt:
        raise llm.CompanionLLMError("rewritten_prompt 是空的")
    # 兜底：模型偷懒没写风格时补上（本地模型对「二次元 / 日系插画」这类词很敏感，
    # 缺了就往半写实跑）。
    if not _style_present(prompt, style):
        prompt = prompt.rstrip("。.；; ") + "；" + style.rstrip("。. ") + "。"
        log.info("提示词里没有风格约束，已自动补上")
    wh_ratio = _one_line(data.get("wh_ratio"))
    ratio_follow = _one_line(data.get("ratio_follow"))
    return {
        "rewritten_prompt": prompt,
        "wh_ratio": wh_ratio,
        "ratio_follow": ratio_follow,
        "raw": json.dumps(data, ensure_ascii=False)[:2000],
    }


def aspect_from_ratio_follow(ratio_follow: str, refs: list[dict[str, Any]],
                             fallback: str) -> str:
    """把「跟某张图」翻译成具体画幅（读那张图的实际长宽比）。"""
    try:
        index = int(str(ratio_follow).strip().strip("<>").replace("image", "") or 0)
    except ValueError:
        return fallback
    if index < 1 or index > len(refs):
        return fallback
    path = refs[index - 1].get("path")
    try:
        from PIL import Image

        with Image.open(Path(path)) as img:
            w, h = img.size
        from math import gcd

        g = gcd(w, h) or 1
        return f"{w // g}:{h // g}"
    except Exception:
        return fallback
