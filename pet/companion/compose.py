# -*- coding: utf-8 -*-
"""把「画面描述 + 关键词 + 库条目」组合成一次生图请求（提示词 + 参考图）。

要点（与 dsh-image-studio 一致）：参考图只负责「长得像」和服装设计，姿势、表情、
取景一律由文字重新安排，否则每张图都会照抄参考图的站姿。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import library, presets as presets_mod

CAT_LABEL = library.CATEGORY_LABEL

#: 「自拍手臂入镜」关掉后，这些写法一律不许留在提示词里。
#: 大模型写画面时特别爱自己补「举着手机」，光在指令里说不要并不够——必须在
#: 出场前按句过滤，否则云端（不带千问重写）会直接把它送进生图接口。
_PHONE_WORDS = (
    "举着手机", "举起手机", "举高手机", "手机自拍", "手机镜头", "手机屏幕",
    "手机挡", "手机在画面", "伸向镜头", "举向镜头", "对着手机", "自拍杆",
    "拿着手机", "握着手机", "捧着手机", "手机对着", "手机举",
    # 云端编辑模型爱写的「自拍视角」变体，前几版词表漏掉这些，导致开关关了
    # 云端照旧画出伸向镜头的手臂。这里补齐「手臂/手/手机 + 镜头」的各种写法。
    "手机视角", "拿起手机", "掏出手机", "掏出手机拍照", "对着镜子拍", "对着后置镜头",
    "手臂伸进画面", "手臂伸出", "手臂从画面", "一条手臂", "整条胳膊", "胳膊伸",
    "手举手机", "举手机", "手机拍自己", "手机在手里", "举着镜头", "手伸向镜头",
    "手臂伸向镜头", "伸进画面", "伸入画面", "从画面外伸进来", "自拍的角度",
)
#: 只删「手机 / 手臂伸向镜头」这类动作短语，不必整句删（保留同一句里的
#: 背景、穿着、表情信息）——用于 traits 等「一句话里混着姿势与神态」的字段。
_PHONE_PHRASE = re.compile(
    r"[^，,。；;、！!？?\n]*(?:举着手机|举起手机|举高手机|拿着手机|握着手机|捧着手机|"
    r"拿起手机|掏出手机|手机视角|手机自拍|自拍杆|手机镜头|手机屏幕|手机挡|手机对着|手机举|"
    r"伸向镜头|举向镜头|手臂伸进画面|手臂伸出|手臂从画面|手臂伸向镜头|一条手臂|整条胳膊|"
    r"胳膊伸|手举手机|举手机|手机拍自己|手机在手里|举着镜头|手伸向镜头|伸进画面|伸入画面|"
    r"从画面外伸进来|对着镜子拍|对着后置镜头)[^，,。；;、！!？?\n]*[，,。；;、！!？?\n]?"
)
_CLAUSE_SPLIT = re.compile(r"(?<=[，,。；;、！!？?\n])")
#: 整句基本只是在说「自拍」的短句（如「她在街上自拍」里「自拍」被删后剩个空壳），
#: 用来判断要不要连这一句一起丢。这里只匹配「就一个自拍/自拍照」的孤立从句。
_ONLY_SELFIE_WORD = re.compile(r"^[，,。；;、！!？?\s]*(?:自拍|自拍照|手机自拍)[，,。；;、！!？?\s]*$")


def strip_phone_wording(text: Any) -> str:
    """去掉「举着手机 / 手机自拍」这类自拍描写，返回只剩画面的句子。

    只在「自拍手臂入镜」关闭时调用：整句含手机动作就丢掉这一句，
    剩下的「自拍」改成「照片」。全被丢光时给一句最普通的近景。
    """
    value = _one_line(text)
    if not value:
        return ""
    kept = [part for part in _CLAUSE_SPLIT.split(value)
            if part and not any(word in part for word in _PHONE_WORDS)
            and not _ONLY_SELFIE_WORD.search(part)]
    out = "".join(kept).strip(" ，,、;；")
    # 开关关着时「自拍」这个词本身就该消失——直接删掉，不要换成「照片」，
    # 否则会留下「她在街上照片」这种读不通的残句。
    out = out.replace("自拍照", "").replace("自拍", "")
    out = " ".join(out.split()).strip(" ，,、;；")
    if not out:
        return "她对着镜头拍了一张照片"
    return out


def strip_phone_phrases(text: Any) -> str:
    """只删「举着手机 / 手伸向镜头 / 手臂伸进画面」这类动作短语，保留同句其余内容。

    用于 ``traits`` 这种「一句话里混着性格神态与自拍姿势」的字段：
    「自拍时会自然对着镜头笑、身体前倾，一只手伸向镜头（手机视角）」应当洗成
    「自拍时会自然对着镜头笑、身体前倾」，而不是整句丢掉。开关关闭时使用。
    """
    value = _one_line(text)
    if not value:
        return ""
    out = _PHONE_PHRASE.sub("", value)
    # 只删手机/手臂动作短语，不把「自拍」硬换成「照片」——traits 里的
    # 「自拍时会对着镜头笑」是性格描写，保留它无害，硬替换反而读起来别扭。
    out = " ".join(out.split())
    return out.strip(" ，,、;；。")


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()][:12]


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


def normalize_aspect(value: Any, fallback: str) -> str:
    s = str(value or "").strip().replace("：", ":").replace("×", "x").replace("*", "x")
    if ":" in s and all(part.isdigit() for part in s.split(":", 1)):
        return s
    if "x" in s.lower():
        return s.lower()
    return fallback


def _keyword(group: str, raw: Any, presets) -> dict[str, Any] | None:
    value = str(raw or "").strip()
    if not value:
        return None
    hit = presets_mod.find_preset(group, value, presets)
    if hit:
        return {"name": hit["name"], "text": hit.get("prompt") or hit["name"], "preset": True}
    return {"name": value, "text": value, "preset": False}


def compose(args: dict[str, Any], store, lib=None, presets=None, now=None) -> dict[str, Any]:
    """返回 {final_prompt, refs, aspect, selected, keywords, notes, unresolved}。"""
    lib = lib if lib is not None else library.scan_all(store)
    presets = presets if presets is not None else presets_mod.load(store)
    notes: list[str] = []
    unresolved: list[str] = []
    arm_on = bool(store.get("companion_qwen_selfie_arm", False))
    prompt = str(args.get("prompt") or "").strip()
    if prompt and not arm_on:
        cleaned = strip_phone_wording(prompt)
        if cleaned != prompt:
            notes.append("画面里的「举着手机」已按设置去掉（自拍手臂入镜＝关）")
        prompt = cleaned

    situation = _keyword("situation", args.get("situation"), presets)
    sit = None
    if situation and situation["preset"]:
        sit = presets_mod.find_preset("situation", situation["name"], presets)

    kw: dict[str, Any] = {}
    for group in ("expression", "pose", "shot", "light"):
        found = _keyword(group, args.get(group), presets)
        if found:
            kw[group] = found
    if "light" not in kw and sit and sit.get("light"):
        kw["light"] = _keyword("light", sit["light"], presets)

    # 「自拍手臂入镜」关（默认）时：不管是模型填了「自拍」、填了库外的「照片」，
    # 还是一句都没填，一律换成普通拍一张照片的取景——提示词里不出现「举起手机」。
    if not arm_on:
        shot = kw.get("shot")
        selfie_like = shot and (shot.get("name") in (presets_mod.ARM_SHOT_NAME,
                                                     presets_mod.PLAIN_SHOT_NAME))
        if not shot or not shot.get("preset") or selfie_like:
            kw["shot"] = {"name": presets_mod.PLAIN_SHOT_NAME,
                          "text": presets_mod.PLAIN_SHOT_TEXT, "preset": True}

    def pick_many(category: str, refs) -> list[dict[str, Any]]:
        out = []
        for ref in refs:
            entry = library.resolve_ref(category, ref, lib)
            if entry is None:
                unresolved.append(f"{CAT_LABEL[category]}「{ref}」")
            elif entry not in out:
                out.append(entry)
        return out

    characters = pick_many("characters", _as_list(args.get("characters")))
    outfits = pick_many("outfits", _as_list(args.get("outfits")))
    scenes = pick_many("scenes", _as_list(args.get("scene")))
    props = pick_many("props", _as_list(args.get("props")))
    if len(scenes) > 1:
        notes.append(f"场景只能用一个，已用「{scenes[0]['name']}」")
    scene = scenes[0] if scenes else None
    text_only: list[str] = []

    if scene is None and sit and sit.get("scene"):
        entry = library.resolve_ref("scenes", sit["scene"], lib)
        if entry is not None:
            scene = entry
            notes.append(f"情境「{sit['name']}」→ 场景「{entry['name']}」")
        else:
            text_only.append(f"场景：{sit['scene']}")

    # 画面描述里提到的库名字自动带上
    auto: list[dict[str, Any]] = []
    if prompt:
        for entry in library.mentioned_in("characters", prompt, lib):
            if entry not in characters:
                characters.append(entry)
                auto.append(entry)
        for entry in library.mentioned_in("outfits", prompt, lib):
            if entry not in outfits:
                outfits.append(entry)
                auto.append(entry)
        if scene is None:
            hits = library.mentioned_in("scenes", prompt, lib)
            if hits:
                scene = hits[0]
                auto.append(scene)
        for entry in library.mentioned_in("props", prompt, lib):
            if entry not in props:
                props.append(entry)
                auto.append(entry)
    if auto:
        notes.append("从画面描述里自动认出：" + "、".join(f"{CAT_LABEL[e['category']]}「{e['name']}」" for e in auto))

    # 服饰兜底：她现在穿的 > 人物的情境穿搭 > 情境默认 > 场景默认 > 人物默认
    dressed = set()
    if not outfits and store.get("companion_follow_schedule", True):
        from . import schedule as schedule_mod

        for character in characters:
            try:
                hit = schedule_mod.outfit_for_now(store, character, scene=scene, now=now)
            except Exception:
                hit = None
            if not hit:
                continue
            dressed.add(character["id"])
            if hit.get("outfit"):
                if hit["outfit"] not in outfits:
                    outfits.append(hit["outfit"])
                if hit.get("text"):
                    text_only.append(f"{character['name']}的穿着细节：{hit['text']}")
                notes.append(f"沿用{character['name']}此刻身上穿的「{hit['outfit']['name']}」"
                             + (f"（{hit['text']}）" if hit.get("text") else ""))
            elif hit.get("text"):
                text_only.append(f"{character['name']}穿着{hit['text']}")
                notes.append(f"沿用{character['name']}此刻身上穿的：{hit['text']}（服饰库里没有，只按文字描述）")

    if not outfits:
        for character in characters:
            if character["id"] in dressed:
                continue
            candidates = []
            if sit and character.get("situationOutfits", {}).get(sit["name"]):
                candidates.append((character["situationOutfits"][sit["name"]], f"「{character['name']}」在情境「{sit['name']}」的穿搭"))
            if sit and sit.get("outfit"):
                candidates.append((sit["outfit"], f"情境「{sit['name']}」的默认服饰"))
            if scene and scene.get("defaultOutfit"):
                candidates.append((scene["defaultOutfit"], f"场景「{scene['name']}」的默认服饰"))
            if character.get("defaultOutfit"):
                candidates.append((character["defaultOutfit"], f"「{character['name']}」的默认服饰"))
            for name, why in candidates:
                entry = library.resolve_ref("outfits", name, lib)
                if entry is not None:
                    if entry not in outfits:
                        outfits.append(entry)
                    notes.append(f"{why}「{entry['name']}」")
                    break
                if name:
                    text_only.append(f"{character['name']}穿着{name}")
                    notes.append(f"{why}「{name}」库里没有，只按文字描述")
                    break

    # 参考图配额
    ordered = list(characters) + list(outfits) + ([scene] if scene else []) + list(props)
    max_refs = int(store.get("companion_max_refs", 8) or 8)
    per_character = max(1, min(4, max_refs))
    per_other = max(1, min(3, max_refs))
    chosen: dict[str, list[Path]] = {entry["id"]: [] for entry in ordered}
    used = 0
    for round_index in range(6):
        for entry in ordered:
            if used >= max_refs:
                break
            quota = per_character if entry["category"] == "characters" else per_other
            picked = chosen[entry["id"]]
            if len(picked) > round_index or len(picked) >= quota:
                continue
            images = entry.get("images") or []
            if round_index >= len(images):
                continue
            picked.append(images[round_index])
            used += 1
    dropped = [e for e in ordered if e.get("images") and not chosen[e["id"]]]
    if dropped:
        notes.append(f"参考图超过上限 {max_refs} 张，没放进去：{'、'.join(e['name'] for e in dropped)}（只按文字描述）")

    refs: list[dict[str, Any]] = []
    for entry in ordered:
        for image in chosen[entry["id"]]:
            refs.append({
                "category": entry["category"],
                "entry": entry["name"],
                "path": library.entry_image_path(entry, image),
            })

    loose = bool(store.get("companion_identity_only", True))
    single = characters[0] if len(characters) == 1 else None
    if loose:
        hints = {
            "characters": "只用来确定长相：脸型五官、发型发色、瞳色、体型比例和标志性特征。参考图里的姿势、表情、视角和背景都不要照搬。",
            "outfits": (f"只参考服装设计（款式、颜色、材质、细节），穿在「{single['name']}」身上，随动作自然起褶变形。"
                        if single else "只参考服装设计（款式、颜色、材质、细节），随人物动作自然起褶变形。"),
            "scenes": "只参考空间、陈设、配色与风格；镜头角度和取景按上面的描述重新安排。",
            "props": "只参考外形、材质与配色。",
        }
    else:
        hints = {
            "characters": "外貌（脸、发型发色、体型、标志性特征）严格以参考图为准。",
            "outfits": (f"给「{single['name']}」穿上这套服饰，款式、材质与配色按参考图还原。"
                        if single else "服饰款式、材质与配色按参考图还原。"),
            "scenes": "画面发生在这个场景里，空间布局、陈设与光线氛围参考此图。",
            "props": "道具外形按参考图还原。",
        }

    parts: list[str] = []
    style = str(store.get("companion_style_prompt", "") or "").strip()
    if style:
        parts.append(f"【画风】{style}")
    parts.append(f"【画面】{prompt}")
    if situation:
        extra = f"：{sit['prompt']}" if sit and sit.get("prompt") else ""
        parts.append(f"【情境】{situation['name']}{extra}")
    for group in ("expression", "pose", "shot", "light"):
        if group in kw:
            label = "光线氛围" if group == "light" else presets_mod.PRESET_LABEL[group]
            parts.append(f"【{label}】{kw[group]['text']}")
    traits = [c for c in characters if (c.get("traits") or "").strip()]
    if traits:
        rows = []
        for c in traits:
            t = _one_line(c["traits"])
            if not arm_on:
                # traits 里常写着「一只手伸向镜头（手机视角）」，云端会当真照画，
                # 开关关着就必须把这句里的手机姿势洗掉，只留性格神态。
                t = strip_phone_phrases(t)
            if t:
                rows.append(f"{c['name']}：{t}")
        if rows:
            parts.append("【人物神态特点】\n" + "\n".join(rows))
    if text_only:
        parts.append("【搭配】" + "；".join(text_only))

    lines: list[str] = []
    counter = 0

    def span(count: int) -> str:
        nonlocal counter
        start = counter + 1
        counter += count
        return f"图{start}" if count == 1 else f"图{start}–{counter}"

    no_image: list[str] = []
    for entry in ordered:
        count = len(chosen[entry["id"]])
        desc = _one_line(entry.get("description"))
        if count == 0:
            no_image.append(f"{CAT_LABEL[entry['category']]}「{entry['name']}」：{desc or '（无描述）'}")
            continue
        line = f"{span(count)}：{CAT_LABEL[entry['category']]}「{entry['name']}」——{hints[entry['category']]}"
        if desc:
            line += f"\n  外观：{desc}"
        lines.append(line)
    if lines:
        parts.append("【参考图说明（按上传顺序编号）】\n" + "\n".join(lines))
    if no_image:
        parts.append("【没有参考图、只按文字描述的元素】\n" + "\n".join(no_image))
    if lines:
        parts.append(
            "要求：画一张全新的画面，不要把参考图拼贴、并排或作为画中画放进去。参考图只负责「长得像」："
            "脸、发型发色、体型、标志性特征、服装款式与配色必须一致。构图、视角、景别、姿势、动作、手势、"
            "朝向、表情、光线方向和背景陈设，一律按上面的【画面】【情境】【神态】【动作】【镜头】【光线氛围】"
            "重新安排，不要沿用任何一张参考图里的站姿、坐姿、表情或取景；两者冲突时以文字为准。"
            if loose else
            "要求：把以上参考融合成一张完整、自然的新画面，不要把参考图拼贴、并排或作为画中画放进画面；人物外貌必须与参考图一致。"
        )

    # 「自拍手臂入镜」关掉时，云端通道（不走千问重写）最容易犯的错：直接照抄
    # 人物参考图本身那张「举着手机自拍」的姿势。这里在提示词最末尾补一段强约束，
    # 明确点名「参考图里伸向镜头的手机和手臂不要带进来」，并把手机/手臂列为负向项。
    if not arm_on:
        parts.append(
            "【重要·参考图姿势不要照搬】人物参考图里可能是举着手机自拍的动作。"
            "这张图不是自拍：画面里不要出现手机、伸向镜头的手臂、自拍杆或任何从画面外伸进来的胳膊/手/袖口；"
            "人物两只手照常出现在该在的位置（放在身侧、端杯子、拿道具都行），镜头自然地对着她。"
        )

    keywords: dict[str, str] = {}
    if situation:
        keywords["situation"] = situation["name"]
    for group in ("expression", "pose", "shot", "light"):
        if group in kw:
            keywords[group] = kw[group]["name"]

    return {
        "final_prompt": "\n\n".join(parts),
        "refs": refs,
        "aspect": normalize_aspect(args.get("aspect"), str(store.get("companion_image_aspect", "3:4") or "3:4")),
        "selected": {
            "characters": [c["name"] for c in characters],
            "outfits": [o["name"] for o in outfits],
            "scene": scene["name"] if scene else "",
            "props": [p["name"] for p in props],
        },
        "keywords": keywords,
        "notes": notes,
        "unresolved": unresolved,
    }


def catalog_text(store, lib=None, max_chars: int = 2400) -> str:
    lib = lib if lib is not None else library.scan_all(store)
    sections = []
    budget = max_chars
    for category in library.CATEGORIES:
        items = lib.get(category) or []
        head = f"{CAT_LABEL[category]}（{category}）"
        if not items:
            sections.append(f"{head}：暂无")
            continue
        rows = []
        shown = 0
        for entry in items:
            alias = f"（又名 {'/'.join(entry['aliases'][:3])}）" if entry["aliases"] else ""
            desc = f" — {_one_line(entry['description'])[:40]}" if entry["description"] else ""
            row = f"{entry['name']}{alias}{desc}"
            if budget - len(row) < 0:
                break
            budget -= len(row) + 2
            rows.append(row)
            shown += 1
        more = f"；…还有 {len(items) - shown} 项" if len(items) > shown else ""
        sections.append(f"{head}：" + "；".join(rows) + more)
    return "\n".join(sections)
