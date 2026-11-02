# -*- coding: utf-8 -*-
"""把那张自拍换成人物库的主参考图（只保留这一张），并补上画风说明。"""
import json
import os
import pathlib
import shutil
import time

from PIL import Image

base = pathlib.Path(os.environ['APPDATA']) / 'dsh-pet-standalone' / 'companion'
lib = base / 'image-library' / 'characters' / '小鱼'
src = base / 'outputs' / '2026-11' / 'selfie-2026-11-01-165312-3379.png'

img = Image.open(src)
print('原图:', src.name, img.size, img.mode, f'{src.stat().st_size // 1024}KB')

# 旧的三视图挪进 .trash（不硬删，和素材库的删除约定一致）
trash = base / 'image-library' / 'characters' / '.trash' / f'小鱼-{time.strftime("%Y%m%d%H%M%S")}'
trash.mkdir(parents=True, exist_ok=True)
for old in sorted(lib.glob('ref-*.*')):
    shutil.move(str(old), str(trash / old.name))
    print('  归档:', old.name, '→', trash.name)

# 新参考图：最长边 1400，存 jpg
target = lib / 'ref-01.jpg'
work = img.convert('RGB')
if max(work.size) > 1400:
    scale = 1400 / max(work.size)
    work = work.resize((round(work.width * scale), round(work.height * scale)), Image.LANCZOS)
work.save(target, 'JPEG', quality=92, optimize=True)
print('新参考图:', target.name, work.size, f'{target.stat().st_size // 1024}KB')

# 条目：cover 指向它，并补一条画风说明（模型据此对齐线条/上色/光照）
entry_path = lib / 'entry.json'
entry = json.loads(entry_path.read_text(encoding='utf-8'))
entry['cover'] = 'ref-01.jpg'
desc = str(entry.get('description') or '')
style_note = ('画面风格：日系精致插画（赛璐璐厚涂混合），暖金色逆光、发丝边缘有通透光晕，'
              '大而亮的蓝色眼睛带高光，皮肤柔和通透，细节刻画精致。')
if '画面风格' not in desc:
    entry['description'] = (desc + '。' if desc and not desc.endswith('。') else desc) + style_note
traits = str(entry.get('traits') or '')
if '自拍' not in traits:
    entry['traits'] = ((traits + '；') if traits else '') + (
        '自拍时会自然对着镜头笑、身体前倾，一只手伸向镜头（手机视角）')
tags = list(entry.get('tags') or [])
for tag in ('画风参考', '日系插画', '暖金逆光'):
    if tag not in tags:
        tags.append(tag)
entry['tags'] = tags
entry['updatedAt'] = time.strftime('%Y-%m-%dT%H:%M:%S')
entry_path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding='utf-8')

print('\n目录:', [p.name for p in sorted(lib.iterdir())])
print('cover:', entry['cover'])
print('description:', entry['description'][:160], '...')
print('traits:', entry['traits'][:120], '...')
