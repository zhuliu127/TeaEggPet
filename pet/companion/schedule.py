# -*- coding: utf-8 -*-
"""每日日程：她每天有自己的一天，聊天和自拍都按「此刻在做什么」。

生成用宿主已配置的模型（``llm.ask``），结果存
``<配置目录>/companion/schedule/days/YYYY-MM-DD.json``；生成失败退化成模板日程，
保证任何时候都有「此刻」可用。
"""

from __future__ import annotations

import json
import logging
import random
from datetime import date as date_cls, datetime, timedelta
from typing import Any

from . import compose as compose_mod
from . import library, llm, presets as presets_mod

log = logging.getLogger(__name__)

WEEK = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

DAY_TYPES = [
    {"id": "workday", "name": "上班日", "enabled": True, "weight": 5, "days": "weekday",
     "guide": "常规上班日，两点一线：起床洗漱、通勤去上班，白天干活，中午吃午饭，傍晚下班回家，晚上吃饭、有一点自己的时间，然后睡觉。"},
    {"id": "restday", "name": "休息日", "enabled": True, "weight": 4, "days": "weekend",
     "guide": "不上班，以待在家里为主：睡个懒觉，在家收拾、打游戏或看剧，出门散散步，去附近的餐馆好好吃一顿。"},
    {"id": "outing", "name": "外出日", "enabled": True, "weight": 2, "days": "weekend",
     "guide": "出门玩一整天：逛街、看展、去公园或海边、找好吃的店，傍晚或晚上才回家，回家时有点累但很满足。"},
    {"id": "homebody", "name": "宅家日", "enabled": True, "weight": 1, "days": "any",
     "guide": "哪儿也不想去，几乎一整天窝在家：睡到中午，点外卖，追剧打游戏，作息有点乱，晚上反而精神。"},
    {"id": "overtime", "name": "加班日", "enabled": True, "weight": 1, "days": "weekday",
     "guide": "活多的一天：照常上班，但一直忙到很晚，午饭晚饭都随便对付，回家已经很累，倒头就睡。"},
    {"id": "slacking", "name": "摸鱼日", "enabled": True, "weight": 1, "days": "weekday",
     "guide": "名义上在上班，实际上一直找机会摸鱼：开小差、刷手机、去茶水间找零食。"},
]


# ---------------------------------------------------------------- 时间
def date_key(d: date_cls | datetime | None = None) -> str:
    d = d or date_cls.today()
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def clock(minutes: int) -> str:
    minutes = max(0, min(1440, int(minutes)))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_clock(value: Any) -> int | None:
    text = str(value or "").strip().replace("：", ":")
    if ":" not in text:
        return None
    head, _, tail = text.partition(":")
    try:
        hour, minute = int(head), int(tail)
    except ValueError:
        return None
    if hour > 24 or minute > 59:
        return None
    if hour == 24:
        return 1440 if minute == 0 else None
    return hour * 60 + minute


def date_label(key: str) -> str:
    try:
        d = date_cls.fromisoformat(key)
    except ValueError:
        return key
    return f"{d.month} 月 {d.day} 日 {WEEK[d.weekday()]}"


def add_days(key: str, delta: int) -> str:
    try:
        return date_key(date_cls.fromisoformat(key) + timedelta(days=delta))
    except ValueError:
        return date_key()


def now_minutes(now: datetime | None = None) -> int:
    now = now or datetime.now()
    return now.hour * 60 + now.minute


# ---------------------------------------------------------------- 形状
def _clean_item(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    start = parse_clock(raw.get("start"))
    end = parse_clock(raw.get("end"))
    if start is None or end is None:
        return None
    activity = " ".join(str(raw.get("activity") or "").split())[:40]
    if not activity:
        return None
    item = {
        "s": start,
        "e": 1440 if (end == 0 and start > 0) else end,
        "activity": activity,
        "location": " ".join(str(raw.get("location") or "").split())[:30],
        "detail": " ".join(str(raw.get("detail") or "").split())[:240],
    }
    for key, limit in (("situation", 20), ("scene", 40), ("outfit", 40), ("outfitNote", 60)):
        value = " ".join(str(raw.get(key) or raw.get("outfit_note") or "").split())[:limit]
        if key == "outfitNote":
            value = " ".join(str(raw.get("outfitNote") or raw.get("outfit_note") or "").split())[:limit]
        if value:
            item[key] = value
    for key in ("props", "with"):
        value = raw.get(key)
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            cleaned = [" ".join(str(x).split())[:40] for x in value if str(x).strip()][:4]
            if cleaned:
                item[key] = cleaned
    return item


def normalize_items(items: Any) -> list[dict[str, Any]]:
    cleaned = [x for x in (_clean_item(i) for i in (items or [])) if x and x["e"] > x["s"]]
    cleaned.sort(key=lambda x: (x["s"], x["e"]))
    out: list[dict[str, Any]] = []
    for item in cleaned:
        if out:
            prev = out[-1]
            if item["e"] <= prev["e"]:
                continue
            if item["s"] < prev["e"]:
                item["s"] = prev["e"]
            elif item["s"] > prev["e"]:
                prev["e"] = item["s"]
        if item["e"] > item["s"]:
            out.append(item)
    if not out:
        return [{"start": "00:00", "end": "24:00", "activity": "在家", "location": "家", "detail": ""}]
    if out[0]["s"] > 0:
        out.insert(0, {"s": 0, "e": out[0]["s"], "activity": "睡觉", "location": "家", "detail": ""})
    if out[-1]["e"] < 1380:
        out.append({"s": out[-1]["e"], "e": 1380, "activity": "在家休息", "location": "家", "detail": ""})
    if out[-1]["e"] < 1440:
        out.append({"s": out[-1]["e"], "e": 1440, "activity": "睡觉", "location": "家", "detail": ""})
    result = []
    for item in out[:40]:
        start, end = clock(item["s"]), clock(item["e"])
        rest = {k: v for k, v in item.items() if k not in ("s", "e")}
        result.append({"start": start, "end": end, **rest})
    return result


def find_index(day: dict[str, Any] | None, minute: int) -> int:
    if not day:
        return -1
    for index, item in enumerate(day.get("items") or []):
        start = parse_clock(item.get("start"))
        end = parse_clock(item.get("end"))
        if start is not None and end is not None and start <= minute < end:
            return index
    return -1


def pick_day_type(types: list[dict[str, Any]], key: str) -> dict[str, Any]:
    try:
        weekday = date_cls.fromisoformat(key).weekday()
    except ValueError:
        weekday = 0
    scope = "weekend" if weekday >= 5 else "weekday"
    enabled = [t for t in types if t.get("enabled", True) and int(t.get("weight") or 0) > 0]
    pool = [t for t in enabled if t.get("days") in (scope, "any")] or enabled
    if not pool:
        return {"id": "restday", "name": "休息日", "guide": "不上班，在家随便过一天。"}
    total = sum(int(t.get("weight") or 0) for t in pool)
    roll = random.uniform(0, total)
    for item in pool:
        roll -= int(item.get("weight") or 0)
        if roll < 0:
            return item
    return pool[-1]


def template_items(day_type: dict[str, Any]) -> list[dict[str, Any]]:
    kind = str(day_type.get("id") or "")

    def it(start, end, activity, location, detail=""):
        return {"start": start, "end": end, "activity": activity, "location": location, "detail": detail}

    if kind in ("workday", "overtime", "slacking"):
        off = "21:00" if kind == "overtime" else "18:00"
        return normalize_items([
            it("00:00", "07:30", "睡觉", "家"),
            it("07:30", "08:15", "起床洗漱、吃早饭", "家"),
            it("08:15", "09:00", "通勤", "路上"),
            it("09:00", "12:00", "上班", "公司"),
            it("12:00", "13:00", "吃午饭", "公司附近"),
            it("13:00", off, "上班", "公司"),
            it(off, "19:45", "下班回家、吃晚饭", "家"),
            it("19:45", "23:00", "自己的时间", "家"),
            it("23:00", "24:00", "睡觉", "家"),
        ])
    if kind == "outing":
        return normalize_items([
            it("00:00", "08:30", "睡觉", "家"),
            it("08:30", "10:00", "起床收拾、出门", "家"),
            it("10:00", "12:30", "逛街", "市中心"),
            it("12:30", "14:00", "吃午饭", "餐馆"),
            it("14:00", "17:30", "到处逛逛", "公园"),
            it("17:30", "19:00", "吃晚饭", "餐馆"),
            it("19:00", "20:00", "回家", "路上"),
            it("20:00", "23:30", "在家休息", "家"),
            it("23:30", "24:00", "睡觉", "家"),
        ])
    return normalize_items([
        it("00:00", "09:30", "睡觉", "家"),
        it("09:30", "10:30", "起床、吃早午饭", "家"),
        it("10:30", "13:00", "在家玩", "家"),
        it("13:00", "15:00", "午睡", "家"),
        it("15:00", "16:30", "出门散步", "附近"),
        it("16:30", "18:00", "在家看剧", "家"),
        it("18:00", "19:30", "吃晚饭", "家"),
        it("19:30", "23:30", "自己的时间", "家"),
        it("23:30", "24:00", "睡觉", "家"),
    ])


# ---------------------------------------------------------------- 衣柜联动
SIT_GUESS = (
    (("睡觉", "入睡", "睡着", "午睡"), ("睡前",)),
    (("起床", "赖床", "醒"), ("刚睡醒",)),
    (("上班", "工作", "写代码", "开会", "干活", "加班", "摸鱼"), ("工作学习",)),
    (("出门", "逛", "散步", "约会", "看展", "公园", "海边", "外出", "咖啡"), ("外出",)),
    (("吃", "饭", "外卖", "零食", "夜宵", "下午茶"), ("吃东西",)),
    (("沙发", "电视", "看剧", "番", "综艺", "电影"), ("客厅放松",)),
    (("游戏", "追剧", "刷手机", "发呆", "休息", "宅"), ("在家休息",)),
)


def _norm(value: Any) -> str:
    return "".join(str(value or "").strip().lower().split())


def resolve_entry(items, ref):
    key = _norm(ref)
    if not key:
        return None
    for item in items:
        if _norm(item["name"]) == key or _norm(item["id"]) == key:
            return item
    for item in items:
        if any(_norm(a) == key for a in item["aliases"]):
            return item
    for item in items:
        if any(len(_norm(n)) >= 2 and (_norm(n) in key or key in _norm(n))
               for n in [item["name"]] + list(item["aliases"])):
            return item
    return None


def guess_situation(item, situations):
    if item.get("situation"):
        hit = presets_mod.find_situation(item["situation"], {"situation": situations})
        if hit:
            return hit
    hay = f"{item.get('activity', '')} {item.get('location', '')}"
    for words, names in SIT_GUESS:
        if any(word in hay for word in words):
            for name in names:
                hit = presets_mod.find_situation(name, {"situation": situations})
                if hit:
                    return hit
    return None


def her_entry(lib, store):
    characters = lib.get("characters") or []
    wanted = _norm(store.get("companion_character_name", ""))
    for entry in characters:
        if _norm(entry["name"]) == wanted or any(_norm(a) == wanted for a in entry["aliases"]):
            return entry
    return characters[0] if characters else None


def wardrobe_context(store):
    lib = library.scan_all(store)
    presets = presets_mod.load(store)
    return lib, presets, presets.get("situation") or []


def link_items(items, store, fill: bool = True) -> list[dict[str, Any]]:
    """把每段的 outfit / scene / props 对到库里，缺的按规则补。"""
    lib, presets, situations = wardrobe_context(store)
    if not any(lib.get(c) for c in library.CATEGORIES):
        return [dict(i) for i in items]
    her = her_entry(lib, store)
    outfits_lib = lib.get("outfits") or []
    scenes_lib = lib.get("scenes") or []
    props_lib = lib.get("props") or []

    def rule_outfit(item, scene_entry):
        sit = guess_situation(item, situations)
        candidates = [
            (her or {}).get("situationOutfits", {}).get(sit["name"]) if sit and her else None,
            (sit or {}).get("outfit"),
            (scene_entry or {}).get("defaultOutfit"),
            (her or {}).get("defaultOutfit"),
        ]
        for name in candidates:
            if not name:
                continue
            found = resolve_entry(outfits_lib, name)
            if found:
                return found["name"]
        return ""

    out: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    prev_rule = ""
    for raw in items:
        item = dict(raw)
        if item.get("scene"):
            found = resolve_entry(scenes_lib, item["scene"])
            item["scene"] = found["name"] if found else ""
        if not item.get("scene") and fill and item.get("location"):
            hits = [s for s in scenes_lib
                    if any(len(_norm(n)) >= 2 and _norm(n) in _norm(item["location"])
                           for n in [s["name"]] + list(s["aliases"]))]
            if len(hits) == 1 and len(_norm(hits[0]["name"])) >= 2:
                item["scene"] = hits[0]["name"]
        given = str(item.get("outfit") or "").strip()
        note = str(item.get("outfitNote") or "").strip()
        found = resolve_entry(outfits_lib, given) if given else None
        item["outfit"] = found["name"] if found else ""
        item["outfitNote"] = note if found else "，".join(x for x in (given, note) if x)
        scene_entry = resolve_entry(scenes_lib, item["scene"]) if item.get("scene") else None
        rule = rule_outfit(item, scene_entry) if fill else ""
        if fill and not item["outfit"] and not item["outfitNote"]:
            if prev and (prev.get("outfit") or prev.get("outfitNote")) and (not rule or rule == prev_rule):
                item["outfit"] = prev.get("outfit", "")
                item["outfitNote"] = prev.get("outfitNote", "")
            elif rule:
                item["outfit"] = rule
        prev_rule = rule
        if fill:
            if item.get("props"):
                names = [resolve_entry(props_lib, p) for p in item["props"]]
                item["props"] = list(dict.fromkeys([n["name"] for n in names if n]))[:3]
        for key in ("outfit", "outfitNote", "scene"):
            if not item.get(key):
                item.pop(key, None)
        if isinstance(item.get("props"), list) and not item["props"]:
            item.pop("props", None)
        out.append(item)
        prev = item
    return out


def outfit_label(item: dict[str, Any]) -> str:
    if not item:
        return ""
    if item.get("outfit") and item.get("outfitNote"):
        return f"「{item['outfit']}」（{item['outfitNote']}）"
    if item.get("outfit"):
        return f"「{item['outfit']}」"
    return str(item.get("outfitNote") or "")


def outfit_for_now(store, character, scene=None, now=None):
    """她现在穿什么（给 compose 的「跟着日程穿衣」用）。"""
    day = read_day(store, date_key(now))
    if not day:
        return None
    index = find_index(day, now_minutes(now))
    if index < 0:
        return None
    item = day["items"][index]
    if not item.get("outfit") and not item.get("outfitNote"):
        return None
    lib = library.scan_all(store)
    outfit = resolve_entry(lib.get("outfits") or [], item["outfit"]) if item.get("outfit") else None
    return {"outfit": outfit, "text": str(item.get("outfitNote") or ""), "item": item}


# ---------------------------------------------------------------- 存储
def day_path(store, key: str):
    return store.days_dir() / f"{key}.json"


def read_day(store, key: str) -> dict[str, Any] | None:
    data = store.read_json(day_path(store, key), None)
    return data if isinstance(data, dict) and data.get("items") else None


def write_day(store, day: dict[str, Any]) -> dict[str, Any]:
    store.write_json(day_path(store, day["date"]), day)
    return day


def recent_summary(store, key: str, days: int = 3) -> str:
    lines = []
    for offset in range(days, 0, -1):
        day = read_day(store, add_days(key, -offset))
        if not day:
            continue
        main = "、".join(i["activity"] for i in day["items"] if "睡" not in i["activity"])[:80]
        worn = "、".join(dict.fromkeys(i.get("outfit", "") for i in day["items"] if i.get("outfit")))
        places = "、".join(dict.fromkeys(i.get("scene", "") for i in day["items"] if i.get("scene")))
        line = f"- {date_label(day['date'])}（{(day.get('dayType') or {}).get('name', '')}）：{day.get('title', '')}"
        if main:
            line += f"。做了：{main}"
        if worn:
            line += f"。穿过：{worn}"
        if places:
            line += f"。去过：{places}"
        lines.append(line)
    return "\n".join(lines)


def persona_text(store) -> str:
    text = str(store.get("companion_persona", "") or "").strip()
    if text:
        return text
    return "你是住在对方电脑里的蓝鲸女孩，说话口语、简短，偶尔犯困，但也有自己的一整天。"


#: 每天「重点安排」的素材条数：库再大也只把这一小撮推到模型眼前，
#: 但按日期轮换，几天下来整个库都会轮到（新加的素材自然进轮换）。
SPOTLIGHT_SCENES = 4
SPOTLIGHT_PROPS = 6
SPOTLIGHT_OUTFITS = 3

#: 提示词里对「新素材怎么用」的示例（只是举例，不在表里也能靠描述推断）。
_USE_HINTS = (
    "望远镜→夜里看星星、看演出；应援棒→演唱会、漫展；痛包→逛漫展；"
    "锅铲→做饭；手冲咖啡壶→煮咖啡；滑板→小区里、公园；"
    "游戏手柄 / 掌机→客厅打游戏；机械键盘→写代码、直播；猫耳耳机→戴耳机打游戏、听歌；"
    "拍立得→拍一张；便当盒→带饭、野餐；香水 / 首饰盒 / choker颈链 / 小鲸鱼发卡→出门前打扮；"
    "复古随身听→通勤路上听歌；平板电脑→追剧、画画；魔法杖→漫展 cos"
)


def _day_ordinal(key: str) -> int:
    try:
        return datetime.strptime(str(key), "%Y-%m-%d").toordinal()
    except (TypeError, ValueError):
        return datetime.now().toordinal()


def recent_usage(store, key: str, days: int = 3) -> dict[str, set[str]]:
    """最近几天已经用过的场景 / 道具 / 服饰（轮换时把它们排到后面）。"""
    used: dict[str, set[str]] = {"scenes": set(), "props": set(), "outfits": set()}
    for offset in range(1, max(1, days) + 1):
        day = read_day(store, add_days(key, -offset))
        if not day:
            continue
        for item in day.get("items") or []:
            if item.get("scene"):
                used["scenes"].add(str(item["scene"]))
            if item.get("outfit"):
                used["outfits"].add(str(item["outfit"]))
            for name in item.get("props") or []:
                used["props"].add(str(name))
    return used


def _rotate(pool: list[dict[str, Any]], ordinal: int, count: int) -> list[dict[str, Any]]:
    if not pool:
        return []
    if count >= len(pool):
        return list(pool)
    start = (ordinal * max(1, count)) % len(pool)
    return [pool[(start + i) % len(pool)] for i in range(count)]


def spotlight(entries, key: str, count: int, *, salt: int = 0,
              avoid=None) -> list[dict[str, Any]]:
    """按日期确定性轮换出一小撮「今天可以用的素材」。

    为什么要有它（用户实测）：把整个库平铺给模型，它照样只挑最顺手的那几样，
    演唱会、猫耳耳机这类「要用得上合适场合」的素材一年都轮不到；而只固定展示前
    N 条时，**后加的素材永远进不了提示词**。按日期错位取窗口，两个问题一起解决：
    每天换一撮、所有条目都会被轮到、新条目加进库就自动进轮换。
    """
    items = [e for e in (entries or []) if str(e.get("name") or "").strip()]
    if not items or count <= 0:
        return []
    ordered = sorted(items, key=lambda e: str(e.get("name") or ""))
    ordinal = _day_ordinal(key) + int(salt)
    if count >= len(ordered):
        return ordered
    # 最近几天用过的排后面：这样「天天在用的那几样」不会一直霸占名额，
    # 冷门素材反而优先被推到模型眼前。
    block = {str(x).strip() for x in (avoid or []) if str(x).strip()}
    fresh = [e for e in ordered if str(e["name"]) not in block]
    stale = [e for e in ordered if str(e["name"]) in block]
    if len(fresh) >= count:
        return _rotate(fresh, ordinal, count)
    picked = list(fresh)
    picked.extend(_rotate(stale, ordinal, count - len(picked)))
    return picked


def wardrobe_prompt(store, key: str = ""):
    """给生成提示词用的衣柜 / 场景 / 道具段落。"""
    lib, presets, situations = wardrobe_context(store)
    if not any(lib.get(c) for c in library.CATEGORIES):
        return "", [], {}
    sections = []
    rules = []
    fields: dict[str, Any] = {}
    outfits = lib.get("outfits") or []
    scenes = lib.get("scenes") or []
    props = lib.get("props") or []
    if outfits:
        rows = [f"- {e['name']}" + (f"（又名 {'/'.join(e['aliases'][:2])}）" if e["aliases"] else "") + (f"：{' '.join(e['description'].split())[:70]}" if e["description"] else "")
                for e in outfits[:30]]
        sections.append("【她的衣柜】（服饰库，每段的 outfit 从这里选，写名字）\n" + "\n".join(rows))
        her = her_entry(lib, store)
        habits = []
        for sit in situations:
            own = (her or {}).get("situationOutfits", {}).get(sit["name"])
            value = own or sit.get("outfit")
            if value:
                habits.append(f"{sit['name']}→{value}")
        for entry in scenes:
            if entry.get("defaultOutfit"):
                habits.append(f"在{entry['name']}→{entry['defaultOutfit']}")
        if her and her.get("defaultOutfit"):
            habits.append(f"平时→{her['defaultOutfit']}")
        if habits:
            sections.append("【她平时的穿搭习惯】" + "；".join(dict.fromkeys(habits)))
        rules.append("- outfit：这一段穿衣柜里的哪一套，写名字。穿搭要连续，只在合理的时候换（起床后、出门前、回到家、洗完澡、睡前）；"
                     "衣柜里没有合适的（睡衣、泳衣、运动服）outfit 留空、在 outfit_note 写穿着什么。")
        pyjamas = next((e for e in outfits if any(k in e["name"] for k in ("睡衣", "睡裙"))), None)
        fields["outfit"] = pyjamas["name"] if pyjamas else ""
        fields["outfit_note"] = "" if pyjamas else "睡衣"
    if scenes:
        rows = [f"- {e['name']}" + (f"：{' '.join(e['description'].split())[:70]}" if e["description"] else "") for e in scenes[:20]]
        sections.append("【常去的地方】（场景库，地点就是这里时 scene 填名字）\n" + "\n".join(rows))
        rules.append("- scene：地点就是上面某个场景时填它的名字；在家尽量落在库里有的房间。")
        bedroom = next((e for e in scenes if any(k in e["name"] for k in ("卧室", "房间"))), None)
        fields["scene"] = bedroom["name"] if bedroom else ""
    if props:
        rows = []
        for e in props[:40]:
            desc = " ".join(str(e.get("description") or "").split())[:50]
            rows.append(f"- {e['name']}" + (f"：{desc}" if desc else ""))
        sections.append("【她的物品】（道具库，用到时 props 填名字）\n" + "\n".join(rows))
        rules.append("- props：用到道具库里的东西时填名字，最多 2 个。")
        fields["props"] = []

    # 今日重点：按日期轮换，库再大也天天在换，新素材自动进轮换；
    # 最近几天用过的先让位（否则「卧室/客厅/睡衣」永远占满）
    used = recent_usage(store, key) if key else {"scenes": set(), "props": set(), "outfits": set()}
    picks = []
    for entry in spotlight(scenes, key, SPOTLIGHT_SCENES, salt=1, avoid=used["scenes"]):
        picks.append(f"- 场景「{entry['name']}」" + (f"：{' '.join(str(entry.get('description') or '').split())[:40]}" if entry.get("description") else ""))
    for entry in spotlight(props, key, SPOTLIGHT_PROPS, salt=2, avoid=used["props"]):
        picks.append(f"- 道具「{entry['name']}」" + (f"：{' '.join(str(entry.get('description') or '').split())[:40]}" if entry.get("description") else ""))
    for entry in spotlight(outfits, key, SPOTLIGHT_OUTFITS, salt=3, avoid=used["outfits"]):
        picks.append(f"- 服饰「{entry['name']}」")
    if picks:
        sections.append(
            "【今天可以重点安排的素材】（库里的东西，按日期轮换；能安排就安排，不合适就跳过，别硬塞）\n"
            + "\n".join(picks)
        )
        rules.append(
            "- 上面【今天可以重点安排的素材】里，把合适的排进真实的活动：用到道具的那一段，activity 要真的在用它；"
            "用到场景的那一段，location 就写那个地方。"
        )
    if scenes or props:
        rules.append(
            "- 素材要和活动对得上，别硬塞：道具只出现在真用得到的那一段（听歌、打游戏、做饭、拍照、出门……），"
            "演唱会 / 漫展 / 商场 / 步行街 / 餐厅 / 咖啡店 / 公园这类地方要有出门和回家。"
            f"例如：{_USE_HINTS}。"
        )
        rules.append(
            "- 这一天至少用到 3 个不同场景、2 件不同道具；如果整天在家，也安排一段用得上库里东西的活动"
            "（烤点东西、泡澡、听歌、整理首饰、拍一张照片）。"
        )
    return "\n".join(sections), rules, fields


def build_prompt(store, key: str, day_type: dict[str, Any], hint: str = "") -> str:
    name = str(store.get("companion_character_name", "她") or "她")
    persona = persona_text(store)
    recent = recent_summary(store, key)
    sections, rules, fields = wardrobe_prompt(store, key)
    example = {"start": "00:00", "end": "07:30", "activity": "睡觉", "location": "家", **fields, "detail": ""}
    lines = [
        f"你在为角色「{name}」安排 {date_label(key)} 这一整天的日程。这份日程会作为 {name} 当天的生活背景，"
        f"聊天和发照片时她按它知道自己此刻在做什么。",
        f"【人设】\n{persona}" if persona else "",
        str(store.get("companion_partner_name", "") or "").strip()
        and f"【用户】{name} 称呼用户为「{store.get('companion_partner_name')}」。",
        f"【这一天的类型】{day_type['name']}：{day_type.get('guide', '')}",
        f"【最近几天】\n{recent}\n这一天的安排、小插曲和穿搭不要和最近几天重复。" if recent else "",
        sections,
        f"【固定设定】{str(store.get('companion_extra_rules', '') or '').strip()}"
        if str(store.get("companion_extra_rules", "") or "").strip() else "",
        f"【这一天的特别要求（优先满足）】{hint}" if hint else "",
        "【要求】",
        "- 从 00:00 排到 24:00，时间连续、不重叠，24 小时制 HH:MM，最后一段 end 写 24:00；一般 12–20 段。",
        "- 每段：activity 写在做什么（具体，10 字以内）；location 写在哪；detail 写一两句当时的状态或小插曲，让这一天有活人感。",
        "- 符合人设的性格和习惯，别夸张成闹剧；睡觉时间合理。",
        *rules,
        "只输出一个 JSON 对象，不要任何其他文字或代码块标记：",
        json.dumps({"title": "一句话概括这一天", "mood": "这一天整体的心情", "items": [example]}, ensure_ascii=False),
    ]
    return "\n".join(x for x in lines if x)


def parse_schedule(text: str) -> dict[str, Any]:
    data = llm.extract_json(text)
    if not isinstance(data, dict):
        raise llm.CompanionLLMError("模型返回的不是对象")
    items = normalize_items(data.get("items"))
    if len(items) < 4:
        raise llm.CompanionLLMError("模型给的日程太短了")
    return {
        "title": " ".join(str(data.get("title") or "").split())[:80],
        "mood": " ".join(str(data.get("mood") or "").split())[:40],
        "items": items,
    }


def generate_day(store, key: str | None = None, hint: str = "", fallback: bool = True,
                 day_types: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    key = key or date_key()
    types = day_types or DAY_TYPES
    day_type = pick_day_type(types, key)
    base = {
        "date": key,
        "dayType": {"id": day_type.get("id"), "name": day_type.get("name")},
        "hint": hint,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        text = llm.ask(
            store.cfg,
            [{"role": "user", "content": build_prompt(store, key, day_type, hint)}],
            max_tokens=4000,
            temperature=0.95,
            timeout=180,
        )
        parsed = parse_schedule(text)
        day = {**base, **parsed, "items": link_items(parsed["items"], store, fill=True),
               "source": "llm", "model": ""}
    except Exception as exc:  # noqa: BLE001
        if not fallback:
            raise
        log.info("日程生成失败，用模板兜底：%s", exc)
        day = {
            **base,
            "title": day_type.get("name", ""),
            "mood": "",
            "items": link_items(template_items(day_type), store, fill=True),
            "source": "template",
            "error": str(exc)[:200],
        }
    return write_day(store, day)


def ensure_day(store, key: str | None = None) -> dict[str, Any]:
    key = key or date_key()
    return read_day(store, key) or generate_day(store, key)


# ---------------------------------------------------------------- 此刻
def current_context(store, now: datetime | None = None) -> dict[str, Any] | None:
    key = date_key(now)
    day = read_day(store, key)
    if not day:
        return None
    index = find_index(day, now_minutes(now))
    if index < 0:
        return None
    item = day["items"][index]
    return {
        "date": key,
        "dayType": (day.get("dayType") or {}).get("name", ""),
        "title": day.get("title", ""),
        "item": item,
        "situation": item.get("situation", ""),
        "scene": item.get("scene", ""),
        "outfit": item.get("outfit", ""),
        "outfitNote": item.get("outfitNote", ""),
        "next": [f"{i['start']} {i['activity']}" for i in day["items"][index + 1:index + 3]],
    }


def chat_context(store, now: datetime | None = None) -> str:
    """给对话用的「她此刻在做什么」：可直接拼进 system 的一段事实。

    背景（用户实测）：问「在干什么」，回答和当天日程（比如「赶新需求加班」）无关——
    聊天侧只注入了时间与人设，日程只喂给了主动对话和自拍。这里把同一份
    ``current_context`` 压成几句话，让聊天也落在这一天里。
    没有今天的日程（未生成 / 关掉了）时返回 ``''``，调用方据此保持原样不注入。
    """
    ctx = current_context(store, now)
    if not ctx:
        return ""
    item = ctx.get("item") or {}
    activity = str(item.get("activity") or "").strip()
    if not activity:
        return ""
    now = now or datetime.now()
    head = f"【你今天的生活】现在是 {clock(now_minutes(now))}（{date_label(ctx['date'])}"
    day_type = str(ctx.get("dayType") or "").strip()
    head += f"，{day_type}）。" if day_type else "）。"
    title = str(ctx.get("title") or "").strip()
    if title and title != activity:
        head += f"今天的主题：{title}。"

    span = f"{item.get('start')}–{item.get('end')}"
    location = str(item.get("location") or "").strip()
    who = f"你在{location}" if location else "你"
    body = f"此刻（{span}）{who}：{activity}"
    detail = " ".join(str(item.get("detail") or "").split())
    if detail:
        body += f"——{detail[:80]}"
    body = body.rstrip("。；，、 ")  # detail 自带句号时别叠成「。。」
    lines = [head, body + "。"]

    wear = str(item.get("outfit") or "").strip()
    note = str(item.get("outfitNote") or "").strip()
    if wear or note:
        lines.append("身上穿的：" + "，".join(x for x in (wear, note) if x) + "。")
    nxt = [str(x) for x in (ctx.get("next") or []) if str(x).strip()]
    if nxt:
        lines.append("接下来：" + "；".join(nxt) + "。")
    lines.append(
        "用户问起你在做什么、在哪、忙不忙、累不累、吃没吃饭、穿什么这类问题时，"
        "以这段为事实，用第一人称口语回答，可以补细节和感受；"
        "但不要声称自己没有生活或一直在陪人聊天，也不要把这份日程逐条念出来。"
    )
    return "\n".join(lines)


def day_text(store, key: str | None = None) -> dict[str, Any] | None:
    day = read_day(store, key or date_key())
    if not day:
        return None
    return {
        "date": day["date"],
        "dayType": (day.get("dayType") or {}).get("name", ""),
        "title": day.get("title", ""),
        "mood": day.get("mood", ""),
        "source": day.get("source", ""),
        "error": day.get("error", ""),
        "items": day["items"],
    }


def minutes_left(store, now: datetime | None = None) -> int | None:
    """此刻这一段还剩多少分钟（溜达时长跟它走）。"""
    ctx = current_context(store, now)
    if not ctx:
        return None
    end = parse_clock(ctx["item"].get("end"))
    if end is None:
        return None
    return max(0, end - now_minutes(now))
