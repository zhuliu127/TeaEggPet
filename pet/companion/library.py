# -*- coding: utf-8 -*-
"""生图参考库（人物 / 服饰 / 场景 / 道具）。

目录约定与 dsh-image-studio 完全一致，可直接指向已有的库目录：
``<库>/<条目>/entry.json + 若干图``、``<库>/<条目>/若干图``、``<库>/某图.png``。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CATEGORIES = ("characters", "outfits", "scenes", "props")
CATEGORY_LABEL = {"characters": "人物", "outfits": "服饰", "scenes": "场景", "props": "道具"}
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
META = "entry.json"


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.replace("，", ",").replace("、", ",").split(",")
    if not isinstance(value, list):
        return []
    return [str(x).strip()[:40] for x in value if str(x).strip()][:30]


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _outfit_map(value: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(value, dict):
        return out
    for key, val in list(value.items())[:60]:
        k = str(key).strip()[:40]
        v = str(val).strip()[:80]
        if k and v:
            out[k] = v
    return out


def list_images(folder: Path) -> list[Path]:
    try:
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXT and not p.name.startswith(".")]
    except OSError:
        return []
    return sorted(files, key=lambda p: p.name.lower())


def _entry(category: str, folder: Path, name: str, meta: dict[str, Any]) -> dict[str, Any]:
    images = list_images(folder)
    cover = str(meta.get("cover") or "")
    if cover:
        ordered = [p for p in images if p.name == cover]
        images = ordered + [p for p in images if p.name != cover]
    return {
        "id": name,
        "category": category,
        "kind": "folder",
        "dir": folder,
        "name": _text(meta.get("name"), 80) or name,
        "aliases": _str_list(meta.get("aliases")),
        "tags": _str_list(meta.get("tags")),
        "description": _text(meta.get("description"), 4000),
        "persona": _text(meta.get("persona"), 8000),
        "traits": _text(meta.get("traits"), 2000),
        "defaultOutfit": _text(meta.get("defaultOutfit"), 120),
        "situationOutfits": _outfit_map(meta.get("situationOutfits")),
        "images": images,
    }


def scan_category(store, category: str) -> list[dict[str, Any]]:
    root = store.library_dir(category)
    try:
        names = sorted([p.name for p in root.iterdir() if not p.name.startswith(".")])
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for name in names:
        path = root / name
        if path.is_dir():
            meta = store.read_json(path / META, {}) or {}
            out.append(_entry(category, path, name, meta if isinstance(meta, dict) else {}))
        elif path.is_file() and path.suffix.lower() in IMAGE_EXT:
            out.append({
                "id": path.stem, "category": category, "kind": "file", "dir": root,
                "name": path.stem, "aliases": [], "tags": [], "description": "", "persona": "",
                "traits": "", "defaultOutfit": "", "situationOutfits": {}, "images": [path],
            })
    return out


def scan_all(store) -> dict[str, list[dict[str, Any]]]:
    return {category: scan_category(store, category) for category in CATEGORIES}


def _norm(value: Any) -> str:
    return "".join(str(value or "").strip().lower().split())


def resolve_ref(category: str, ref: Any, lib) -> dict[str, Any] | None:
    key = _norm(ref)
    if not key:
        return None
    items = lib.get(category) or []
    for item in items:
        if _norm(item["id"]) == key or _norm(item["name"]) == key:
            return item
    for item in items:
        if any(_norm(a) == key for a in item["aliases"]):
            return item
    loose = [item for item in items
             if any(len(_norm(n)) >= 2 and (_norm(n) in key or key in _norm(n))
                    for n in [item["id"], item["name"]] + list(item["aliases"]))]
    return loose[0] if len(loose) == 1 else None


def mentioned_in(category: str, text: str, lib) -> list[dict[str, Any]]:
    hay = _norm(text)
    if not hay:
        return []
    out = []
    for item in lib.get(category) or []:
        for name in [item["name"]] + list(item["aliases"]):
            key = _norm(name)
            if len(key) >= 2 and key in hay:
                out.append(item)
                break
    return out


def entry_image_path(entry: dict[str, Any], file: str | Path) -> Path:
    """条目里某张图的真实路径。

    ``images`` 里既可能是相对条目目录的文件名，也可能已经是绝对路径（扫描时
    两种都会出现），所以这里先试拼接、再退回原样，避免拼成
    ``…/角色/深深/…/角色/深深/ref-01.png`` 这种不存在的路径（那样参考图会一张都发不出去）。
    """
    path = Path(file)
    joined = Path(entry["dir"]) / path
    if joined.is_file():
        return joined
    if path.is_file():
        return path
    return joined


def ensure_dirs(store) -> None:
    for category in CATEGORIES:
        try:
            store.library_dir(category)
        except OSError:
            log.exception("创建素材库目录失败：%s", category)


def copy_images(target_dir: Path, files) -> list[Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for index, src in enumerate(files, start=1):
        src = Path(src)
        if not src.is_file():
            continue
        dest = target_dir / f"ref-{index:02d}{src.suffix.lower()}"
        try:
            shutil.copyfile(src, dest)
            written.append(dest)
        except OSError:
            log.exception("复制参考图失败：%s", src)
    return written


# ---------------------------------------------------------------- 写入（管理界面用）
def _safe_name(name: str) -> str:
    bad = '<>:"/\\|?*'
    cleaned = "".join(" " if ch in bad or ord(ch) < 32 else ch for ch in str(name or ""))
    cleaned = " ".join(cleaned.split()).strip().strip(".")
    return cleaned[:80]


def _unique_dir(root: Path, name: str) -> str:
    base = _safe_name(name) or "entry"
    candidate = base
    index = 2
    while (root / candidate).exists():
        candidate = f"{base} {index}"
        index += 1
    return candidate


def entry_meta(entry: dict[str, Any]) -> dict[str, Any]:
    """把扫描出来的条目整理成表单要的字段（编辑界面直接绑）。"""
    return {
        "name": entry.get("name", ""),
        "aliases": list(entry.get("aliases") or []),
        "tags": list(entry.get("tags") or []),
        "description": entry.get("description", ""),
        "persona": entry.get("persona", ""),
        "traits": entry.get("traits", ""),
        "defaultOutfit": entry.get("defaultOutfit", ""),
        "situationOutfits": dict(entry.get("situationOutfits") or {}),
    }


def _write_meta(folder: Path, meta: dict[str, Any]) -> None:
    import json

    folder.mkdir(parents=True, exist_ok=True)
    (folder / META).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def save_entry(store, category: str, entry_id: str | None, data: dict[str, Any],
               files=None) -> dict[str, Any]:
    """新建或更新一个条目；files 是新增参考图的源路径（复制进条目目录）。"""
    root = store.library_dir(category)
    folder = root / entry_id if entry_id else root / _unique_dir(root, data.get("name") or "条目")
    folder.mkdir(parents=True, exist_ok=True)
    meta = store.read_json(folder / META, {}) or {}
    if not isinstance(meta, dict):
        meta = {}
    meta.update({
        "name": str(data.get("name") or folder.name).strip()[:80] or folder.name,
        "aliases": [str(x).strip()[:40] for x in (data.get("aliases") or []) if str(x).strip()][:30],
        "tags": [str(x).strip()[:40] for x in (data.get("tags") or []) if str(x).strip()][:30],
        "description": str(data.get("description") or "")[:4000],
        "persona": str(data.get("persona") or "")[:8000],
        "traits": str(data.get("traits") or "")[:2000],
        "defaultOutfit": str(data.get("defaultOutfit") or "")[:120],
        "situationOutfits": {str(k)[:40]: str(v)[:80] for k, v in (data.get("situationOutfits") or {}).items()
                             if str(k).strip() and str(v).strip()},
        "updatedAt": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    })
    if files:
        existing = len(list_images(folder))
        for offset, src in enumerate(files, start=1):
            src = Path(src)
            if not src.is_file():
                continue
            suffix = src.suffix.lower() if src.suffix.lower() in IMAGE_EXT else ".png"
            dest = folder / f"ref-{existing + offset:02d}{suffix}"
            try:
                dest.write_bytes(src.read_bytes())
            except OSError:
                log.exception("复制参考图失败：%s", src)
    _write_meta(folder, meta)
    return _entry(category, folder, folder.name, meta)


def add_images(store, category: str, entry_id: str, files) -> dict[str, Any]:
    root = store.library_dir(category)
    folder = root / entry_id
    existing = len(list_images(folder))
    for offset, src in enumerate(files, start=1):
        src = Path(src)
        if not src.is_file():
            continue
        suffix = src.suffix.lower() if src.suffix.lower() in IMAGE_EXT else ".png"
        try:
            (folder / f"ref-{existing + offset:02d}{suffix}").write_bytes(src.read_bytes())
        except OSError:
            log.exception("复制参考图失败：%s", src)
    meta = store.read_json(folder / META, {}) or {}
    return _entry(category, folder, entry_id, meta if isinstance(meta, dict) else {})


def delete_entry(store, category: str, entry_id: str) -> bool:
    """删除条目：挪进库里 .trash（不硬删，和 dsh-image-studio 一致）。"""
    root = store.library_dir(category)
    folder = root / entry_id
    if not folder.exists():
        return False
    trash = root / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    target = trash / f"{entry_id}-{__import__('time').strftime('%Y%m%d%H%M%S')}"
    try:
        folder.rename(target)
        return True
    except OSError:
        log.exception("删除条目失败：%s", folder)
        return False
