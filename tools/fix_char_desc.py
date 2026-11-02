# -*- coding: utf-8 -*-
"""人物条目收尾：描述里去掉「穿什么」，服装交给服饰参考图；提示词侧加画风基准。"""
import json
import os
import pathlib
import re

base = pathlib.Path(os.environ['APPDATA']) / 'dsh-pet-standalone' / 'companion'
entry_path = base / 'image-library' / 'characters' / '小鱼' / 'entry.json'
entry = json.loads(entry_path.read_text(encoding='utf-8'))
desc = str(entry.get('description') or '')

# 「身上是……小皮鞋。」这一段是三视图里的默认服装，留着会让模型永远画女仆裙
cleaned = re.sub(r'身上是.*?小皮鞋。', '', desc)
entry['description'] = cleaned.strip()
entry_path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding='utf-8')
print('description →', entry['description'])
