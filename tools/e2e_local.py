# -*- coding: utf-8 -*-
"""端到端：真库 + 真 LLM 提示词增强 + 本地 ComfyUI 出图（隔离数据目录，不动用户配置）。

验证三件事：
1. 生成方式 = 本地 → 走 comfyui，不走网关；
2. 提示词里带风格约束（二次元 / 日系插画…）；
3. exact_size 生效 → 出图尺寸等于画幅算出来的像素。
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, '.')

SRC = Path(os.environ['APPDATA']) / 'dsh-pet-standalone'
TMP = Path('build/e2e20')
shutil.rmtree(TMP, ignore_errors=True)
(TMP / 'dsh-pet-standalone' / 'companion').mkdir(parents=True)
shutil.copytree(SRC / 'companion' / 'image-library',
                TMP / 'dsh-pet-standalone' / 'companion' / 'image-library')

base = json.load((SRC / 'config.json').open(encoding='utf-8'))
base.update({
    'companion_image_provider': 'comfyui',
    'companion_comfy_steps': 30,
    'companion_comfy_exact_size': True,
    'companion_qwen_enhance': True,
    'companion_image_aspect': '3:4',
    'companion_comfy_pixels': 780000,
    'companion_qwen_selfie_arm': os.environ.get('PET_ARM', '0') == '1',
})
json.dump(base, (TMP / 'dsh-pet-standalone' / 'config.json').open('w', encoding='utf-8'),
          ensure_ascii=False)

from pet.config import Config                      # noqa: E402
from pet.companion.store import CompanionStore     # noqa: E402
from pet.companion import schedule, selfie         # noqa: E402

cfg = Config(TMP)
store = CompanionStore(cfg)
dst = store.days_dir()
dst.mkdir(parents=True, exist_ok=True)
for f in (SRC / 'companion' / 'schedule' / 'days').glob('*.json'):
    shutil.copy(f, dst / f.name)

print('provider =', store.get('companion_image_provider'), flush=True)
print('此刻 =', (schedule.current_context(store) or {}).get('item'), flush=True)

PLAN = {
    'caption': '客厅灯有点暗～', 'monologue': '拍好啦',
    'prompt': '盘腿坐在客厅地毯上打游戏，屏幕的冷光打在脸上',
    'situation': '', 'expression': '放松', 'pose': '盘腿坐着', 'shot': '自拍',
    'light': '屏幕冷光', 'characters': [], 'outfits': [], 'scene': '', 'props': [],
    'aspect': '3:4',
}
selfie.plan = lambda *a, **k: dict(PLAN, planned=True)

t = time.time()
rec = selfie.generate(store, '', reason='manual',
                      on_status=lambda s: print('  …', s, flush=True))
elapsed = time.time() - t

prompt = rec.get('prompt') or ''
print(f'\n用时 {elapsed:.1f}s', flush=True)
print('素材 =', rec.get('selected'), flush=True)
print('meta =', rec.get('meta'), flush=True)
print('prompt 尾部 =', prompt[-160:], flush=True)
print('含风格关键词 =', any(k in prompt for k in ('二次元', '日系插画', '插画')), flush=True)

from PIL import Image                              # noqa: E402

img = Image.open(rec['path'])
print('出图尺寸 =', img.size, flush=True)
Path('build/e2e20.png').write_bytes(Path(rec['path']).read_bytes())
Path('build/e2e20-prompt.txt').write_text(prompt, encoding='utf-8')
