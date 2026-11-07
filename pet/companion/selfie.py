# -*- coding: utf-8 -*-
"""一张自拍：LLM 想画面 → 组合参考图 → 生图接口 → 落到本地记录。"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime
from typing import Any, Callable

from . import compose as compose_mod
from . import imagegen, library, llm, presets as presets_mod, schedule

log = logging.getLogger(__name__)

WALK_LINES = (
    "我出去溜达一下～",
    "屋里有点闷，我出门走走。",
    "去买点吃的，马上回来。",
    "外面风好舒服，我转一圈。",
)

PLAN_KEYS = ("caption", "monologue", "prompt", "situation", "expression", "pose", "shot", "light", "scene", "aspect")


def _fallback_plan(store, ctx, reason: str) -> dict[str, Any]:
    item = (ctx or {}).get("item") or {}
    activity = item.get("activity") or "在外面散步"
    place = item.get("scene") or item.get("location") or "街上"
    detail = item.get("detail") or ""
    sleeping = "睡" in activity
    if reason == "walk":
        prompt = "她临时出门溜达，走在附近的街上，随手举起手机自拍，背景是街景和行人"
        caption = "出来走走，风很舒服～"
        situation = "外出"
    elif sleeping:
        prompt = "她刚睡醒，坐在床边伸懒腰，头发有点乱，眼神还带着困意"
        caption = "还没完全醒过来…"
        situation = "刚睡醒"
    else:
        prompt = f"{place}，{activity}" + (f"，{detail}" if detail else "") + "，随手举起手机拍了一张"
        caption = "随手拍了一张～"
        situation = "外出" if any(k in activity for k in ("出门", "逛", "散步", "走")) else "在家休息"
    her = schedule.her_entry(library.scan_all(store), store)
    return {
        "caption": caption,
        "monologue": "我回来啦",
        "prompt": prompt,
        "situation": situation,
        "expression": "困倦" if sleeping and reason != "walk" else "微笑",
        "pose": "伸懒腰" if sleeping and reason != "walk" else "站着",
        "shot": "自拍",
        "light": "",
        "characters": [her["name"]] if her else [],
        "outfits": [ctx["outfit"]] if ctx and ctx.get("outfit") else [],
        "scene": (ctx or {}).get("scene") or "",
        "props": [],
        "aspect": str(store.get("companion_image_aspect", "3:4") or "3:4"),
        "planned": False,
    }


def _plan_prompt(store, ctx, hint: str, reason: str) -> str:
    lib = library.scan_all(store)
    presets = presets_mod.load(store)
    her = schedule.her_entry(lib, store)
    name = str(store.get("companion_character_name", "她") or "她")
    item = (ctx or {}).get("item")
    lines = [
        f"你在给桌宠「{name}」安排一张自拍：她{'刚出门溜达了一圈' if reason == 'walk' else '想给你发张照片'}。",
        f"她的性格设定：{schedule.persona_text(store)}",
        (f"她原本的安排：{item.get('start')}–{item.get('end')} {item.get('activity')}"
         f"（{item.get('scene') or item.get('location') or '家'}）" + (f"——{item.get('detail')}" if item.get("detail") else ""))
        if item else "她今天还没有日程，就当她在家附近随便走走。",
        "注意：她现在是临时出门溜达了，不在原本那个地方，画面要写成她在外面（街上、路上、店里、公园之类）。" if reason == "walk" else "",
        (f"她身上穿着：{ctx.get('outfit') or ctx.get('outfitNote')}（这张照片必须是这一套，不要换）")
        if ctx and (ctx.get("outfit") or ctx.get("outfitNote")) else "",
        (f"这一段原本在：{ctx.get('scene')}（场景库里有这个场景；画面要在同一个地方，只是人临时出来了）")
        if ctx and ctx.get("scene") else "",
        f"这次额外要求：{hint}" if hint else "",
        "【要做的事】",
        "按她此刻在做什么，想一个自然的自拍画面，然后填下面的字段：",
        "- prompt：这张照片里发生了什么（40–90 字）。具体到地点细节、手里拿着什么、和周围怎么互动、情绪重点。不要写「自拍」「镜头」这类词，那是 shot 的活。",
        "- caption：她把这张照片发给你时配的一句话，口语，15 字以内，可以带「～」。",
        "- monologue：她回家时说的第一句话，15 字以内。",
        "- shot 一般填「自拍」；其它字段从下面的关键词里挑名字，没有合适的就留空。",
        "- characters / outfits / scene / props 填参考素材库里的名字（留空会自动搭配）。",
        "【可用关键词】",
        presets_mod.keyword_text(presets),
        "【参考素材库】",
        compose_mod.catalog_text(store, lib, 2000),
        "只输出一个 JSON 对象，不要代码块标记：",
        json.dumps({
            "caption": "风好大，头发都飞起来了～",
            "monologue": "我回来啦，给你看照片！",
            "prompt": "傍晚的便利店门口，她一手提着刚买的关东煮，一手举着手机，暖黄的灯牌在身后",
            "situation": "外出", "expression": "开心", "pose": "站着", "shot": "自拍", "light": "霓虹夜景",
            "characters": [her["name"]] if her else [], "outfits": [], "scene": "", "props": [],
            "aspect": str(store.get("companion_image_aspect", "3:4") or "3:4"),
        }, ensure_ascii=False),
    ]
    return "\n".join(x for x in lines if x)


def plan(store, ctx, hint: str = "", reason: str = "walk") -> dict[str, Any]:
    base = _fallback_plan(store, ctx, reason)
    try:
        text = llm.ask(store.cfg, [
            {"role": "system", "content": "你是一个会写画面的插画导演，只输出 JSON。"},
            {"role": "user", "content": _plan_prompt(store, ctx, hint, reason)},
        ], max_tokens=1200, temperature=1.05, timeout=90)
        data = llm.extract_json(text)
        if not isinstance(data, dict):
            return base
    except Exception as exc:  # noqa: BLE001
        log.info("自拍计划失败，用兜底：%s", exc)
        base["planError"] = str(exc)[:200]
        return base
    merged = dict(base)
    for key in PLAN_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            merged[key] = value.strip()[:600 if key == "prompt" else 120]
    for key in ("characters", "outfits", "props"):
        value = data.get(key)
        if isinstance(value, list) and value:
            merged[key] = [str(v).strip() for v in value if str(v).strip()][:4]
    merged["planned"] = True
    return merged


def _save(store, data: bytes, ext: str) -> tuple[Any, str]:
    stamp = datetime.now()
    folder = store.outputs_dir() / f"{stamp.year:04d}-{stamp.month:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"selfie-{schedule.date_key(stamp)}-{stamp:%H%M%S}-{random.randint(1000, 9999)}{ext}"
    path = folder / name
    path.write_bytes(data)
    return path, str(path.relative_to(store.outputs_dir())).replace("\\", "/")


def history(store) -> list[dict[str, Any]]:
    items = store.read_json(store.outputs_dir() / "index.json", {"items": []})
    return items.get("items", []) if isinstance(items, dict) else []


def record(store, rec: dict[str, Any]) -> dict[str, Any]:
    items = history(store)
    items.insert(0, rec)
    store.write_json(store.outputs_dir() / "index.json", {"items": items[:200]})
    return rec


def generate(store, hint: str = "", reason: str = "walk",
             on_status: Callable[[str], None] | None = None,
             overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """拍一张，返回记录（含 path / caption / prompt）。失败抛异常。"""
    ctx = schedule.current_context(store)
    if ctx is None:
        try:
            schedule.ensure_day(store)
            ctx = schedule.current_context(store)
        except Exception:
            ctx = None
    if on_status:
        on_status("她在想拍什么…")
    planned = plan(store, ctx, hint, reason)
    if on_status:
        on_status("举起手机…")
    # 日程 × 素材 联动：这一段原本在哪个场景、穿哪套、拿什么，模型没写就按日程补上
    item = (ctx or {}).get("item") or {}
    lib = library.scan_all(store)
    her = schedule.her_entry(lib, store)
    linked = {}
    characters = planned.get("characters") or ([her["name"]] if her else [])
    outfits = list(planned.get("outfits") or [])
    if not outfits and ctx and (ctx.get("outfit") or ctx.get("outfitNote")):
        outfits = [ctx.get("outfit") or ctx.get("outfitNote")]
        linked["outfit"] = outfits[0]
    scene = planned.get("scene") or ""
    if not scene and (ctx or {}).get("scene"):
        scene = ctx["scene"]
        linked["scene"] = scene
    props = list(planned.get("props") or [])
    if not props and item.get("props"):
        props = list(item["props"])
        linked["props"] = props
    # 照片卡上的「换素材」：用户手选的衣服 / 场景 / 道具优先于模型与日程
    overrides = overrides or {}
    if overrides.get("outfits"):
        outfits = [str(x) for x in overrides["outfits"] if str(x).strip()]
        if outfits:
            linked["outfit"] = outfits[0]
    if overrides.get("scene"):
        scene = str(overrides["scene"]).strip()
        if scene:
            linked["scene"] = scene
    if overrides.get("props"):
        props = [str(x) for x in overrides["props"] if str(x).strip()]
        if props:
            linked["props"] = props

    situation = planned.get("situation") or (ctx or {}).get("situation") or ""
    if not situation and item:
        hit = schedule.guess_situation(item, (presets_mod.load(store).get("situation") or []))
        situation = (hit or {}).get("name", "")
        if situation:
            linked["situation"] = situation
    composed = compose_mod.compose({
        "prompt": planned.get("prompt"),
        "situation": situation,
        "expression": planned.get("expression"),
        "pose": planned.get("pose"),
        "shot": planned.get("shot"),
        "light": planned.get("light"),
        "characters": characters,
        "outfits": outfits,
        "scene": scene,
        "props": props,
        "aspect": planned.get("aspect"),
    }, store)
    refs_for_send = composed["refs"]
    if str(store.get("companion_image_provider", "")) == "comfyui" and refs_for_send:
        # 本机 8G 显存：参考图多于 4 张会慢到没法用，而且同类重复只会让模型糊涂。
        # 每类只留第一张（人物 / 服饰 / 场景 / 道具各一张），顺序也就是「长相 → 衣服 → 环境 → 道具」。
        use_outfit_ref = bool(store.get("companion_comfy_use_outfit_ref", True))
        picked, seen = [], set()
        for ref in refs_for_send:
            category = str(ref.get("category") or "")
            if category in seen:
                continue
            if category == "outfits" and not use_outfit_ref:
                continue
            seen.add(category)
            picked.append(ref)
        refs_for_send = picked
        if not use_outfit_ref:
            # 不带服饰图时，把衣柜里的外观描述写进提示词（本地模型更认「文字说穿什么」）
            for name in (composed.get("selected") or {}).get("outfits") or []:
                entry = library.resolve_ref("outfits", name, lib)
                if entry and entry.get("description"):
                    composed["final_prompt"] += (
                        "\n\n【服装（按文字执行，必须这一套）】" + " ".join(str(entry["description"]).split())
                    )
                    break
        composed["notes"].append("本地通道只带每个分类的第一张参考图（显存友好）")
    prompt_final = composed["final_prompt"]
    aspect_final = composed["aspect"]
    provider = str(store.get("companion_image_provider", "") or "")
    if provider == "comfyui" and refs_for_send and store.get("companion_qwen_enhance", True):
        # 千问编辑模型吃的是「<image1> 是人物、<image2> 是场景、把 A 换成 B」这种单段指令。
        # 我们自己的那段说明书式提示词它读不懂，只会照着参考图糊——
        # 这里交给模板驱动的模型重写一遍，画质差别很明显。
        try:
            from . import qwen_prompt

            refs_detail = []
            for ref in refs_for_send:
                entry = library.resolve_ref(str(ref.get("category")), str(ref.get("entry")), lib)
                refs_detail.append({**ref, "entry_data": entry or {}})
            enh = qwen_prompt.enhance(
                store, refs=refs_detail, item=item, plan=planned,
                aspect=aspect_final,
                weekday=str((ctx or {}).get("dayType") or ""),
                arm_visible=bool(store.get("companion_qwen_selfie_arm", False)),
            )
            prompt_final = enh["rewritten_prompt"]
            if enh.get("wh_ratio"):
                aspect_final = enh["wh_ratio"]
            elif enh.get("ratio_follow"):
                aspect_final = qwen_prompt.aspect_from_ratio_follow(
                    enh["ratio_follow"], refs_for_send, aspect_final)
            composed["notes"].append("提示词已按千问编辑模板重写（" + (enh.get("wh_ratio") or enh.get("ratio_follow") or "跟随设置") + "）")
        except Exception as exc:  # noqa: BLE001
            log.info("提示词增强失败，用原始描述：%s", exc)
            composed["notes"].append(f"提示词增强失败，用原始描述：{str(exc)[:80]}")
    if not refs_for_send and any(lib.get(c) for c in library.CATEGORIES):
        composed["notes"].append("素材库里有条目但一张参考图都没带上（检查条目目录里有没有图片）")
    data, ext, meta = imagegen.generate(store, prompt_final,
                                        [r["path"] for r in refs_for_send],
                                        aspect_final, on_status=on_status)
    path, rel = _save(store, data, ext)
    rec = {
        "id": f"img-{int(time.time() * 1000)}",
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "stamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "reason": reason,
        "path": str(path),
        "rel": rel,
        "caption": planned.get("caption", ""),
        "monologue": planned.get("monologue", ""),
        "brief": planned.get("prompt", ""),
        "prompt": prompt_final,
        "keywords": composed["keywords"],
        "selected": composed["selected"],
        "swap": dict(overrides or {}),
        "notes": composed["notes"] + [f"未找到：{x}" for x in composed["unresolved"]],
        "meta": meta,
        "planned": bool(planned.get("planned")),
        "planError": planned.get("planError", ""),
        "linked": linked,
        "schedule": {
            "activity": item.get("activity", ""),
            "location": item.get("scene") or item.get("location", ""),
            "outfit": ctx.get("outfit") or ctx.get("outfitNote", "") if ctx else "",
            "dayType": (ctx or {}).get("dayType", ""),
            "span": f"{item.get('start', '')}–{item.get('end', '')}" if item else "",
        } if ctx else None,
    }
    return record(store, rec)
