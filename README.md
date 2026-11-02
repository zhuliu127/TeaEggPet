# TeaEggPet · 茶叶蛋桌宠

一只住在桌面上的 AI 陪伴桌宠。她会过自己的每一天，按快捷键出门溜达，回来给你发一张自拍——
照片用**你自己的模型**画：云端 API 或者本机 ComfyUI（Qwen-Image 2.1 图生图），都行。

本项目基于 [dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop)（PySide6 桌宠）二次开发，
在其上新增了一个独立包 `pet/companion/`，也就是下面这些「陪伴扩展」能力；宿主本体（窗口、动画、
聊天、语音、设置）保持原样。

> 角色默认叫「茶叶蛋」。名字、人设、素材库都可以自己改。

---

## 功能

### 陪伴扩展（本项目新增）

| 能力 | 说明 |
| --- | --- |
| **出去溜达 + 自拍** | 按全局快捷键出门，桌宠在桌面上走动；到点或再按一次回家，按「此刻的日程」生成一张自拍 |
| **每日日程** | 用你配的大模型生成她的一整天（几点做什么、在哪、穿什么、拿什么），可手改 |
| **生图素材库** | 人物 / 服饰 / 场景 / 道具四类参考图 + 关键词（情境·神态·动作·镜头·光线） |
| **日程 × 素材联动** | 日程表里「场景 / 穿搭 / 道具」直接选素材库条目；生成时按这一行挑参考图，照片卡上写明用了哪些 |
| **本地 ComfyUI 通道** | Qwen-Image 2.1 图生图，多图参考；不用 Key、不出网。可设置 ComfyUI 目录、扫描本地模型、一键启动 |
| **云端通道** | 65535 异步任务 / OpenAI 兼容（`/v1/images`），网关不支持某条通道时自动换一条再试 |
| **提示词增强** | 本地通道生图前，用你配的模型按「千问编辑模板」把场景改写成 `<image1>…` 打头的编辑指令，并强制带上画风约束 |

### 宿主本体（随 dsh-pet-indesktop 提供）

桌宠本体（精灵动画、桌面走动、气泡、右键菜单、AI 对话、语音报时、余额查询、设置页等）。

---

## 下载

到 [Releases](../../releases) 下载 `TeaEggPet-win64.zip`，解压后双击 `TeaEggPet\TeaEggPet.exe`。
不用装 Python；数据存在 `%APPDATA%\dsh-pet-standalone`。

第一次运行 Windows 可能提示「未知发布者」，点「仍要运行」。

### 首次配置

1. **对话模型**：右键桌宠 → 桌宠设置 → AI 对话，填任意 OpenAI 兼容服务的 Base URL / API Key / 模型名。
   聊天、日程生成、提示词重写都用它。
2. **生图**：右键 → 陪伴扩展 → 陪伴扩展设置… → 生图接口 →「生成方式」二选一：
   - 云端 API：填 Base URL / API Key / 模型；
   - 本地模型：填 ComfyUI 目录 → 扫描本地模型 → 启动 ComfyUI（详见下一节）。

---

## 本地 ComfyUI（Qwen-Image 2.1）

1. **ComfyUI 目录**：整合包根目录（含 `python/` 与 `ComfyUI/`）或 ComfyUI 文件夹本身（含 `main.py`）都认。
2. **扫描本地模型**：不用先启动服务，直接读 `models/diffusion_models`（或 `unet`）、
   `models/text_encoders`（或 `clip`）、`models/vae`，把三个下拉填好。
3. **启动 ComfyUI**：用该目录把服务拉起（独立进程，桌宠退出后它继续跑），日志在数据目录 `comfyui.log`。

需要的模型：

```
models/diffusion_models/  qwen_image_2.1_bf16.safetensors  或  qwen_image_2.1_int8_convrot.safetensors
models/text_encoders/     qwen3vl_8b_bf16.safetensors      或  qwen3vl_8b_int8_convrot.safetensors
models/vae/               qwen_image_2.1_vae_bf16.safetensors
```

**三个模型必须成套**：`unet qwen_image_2.1_*` ↔ `clip qwen3vl_8b_*`（4096 维）↔ `vae qwen_image_2.1_vae_*`。
用 `qwen3vl_32b_*`（5120 维）配 Qwen-Image 会直接报
`Given normalized_shape=[4096] ... got input of size [1, N, 5120]`。
程序会在生成前自检并提示改哪个下拉，不会白跑几十步。

显存参考：8G 用 int8 + 20~30 步（约 2 分钟一张）；16G 以上可用 bf16（更细腻，2~4 分钟）。

---

## 快捷键

| 动作 | 默认键 |
| --- | --- |
| 出去溜达 / 叫她回家 | `Alt+F1` |
| 发一张自拍（按此刻日程） | `Ctrl+Shift+S` |
| AI 对话 / 看看屏幕 | `Alt+F2` / `Alt+F3` |

都在「桌宠设置 → 互动 → 快捷键」里改。

---

## 素材库

右键 → 陪伴扩展 → 素材库与关键词…，四个库：

- **人物**：1~3 张参考图（正/侧/背最好）+ 外观描述、神态特点、人设。**人物图的画风也是整张图的画风**。
- **服饰 / 场景 / 道具**：每个条目 1~2 张参考图 + 外观描述。
- **关键词**：情境 / 神态 / 动作 / 镜头 / 光线。

目录结构与 [dsh-image-studio](https://github.com/aa1355721445-lgtm/dsh-image-studio) 一致，可以直接指向已有的库。

---

## 从源码运行

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pet
```

**注意**：角色素材（`assets/`，约 120MB 的精灵动画）不在本仓库里，需要从
[dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop) 获取后放到项目根的 `assets/` 下，
或直接用 Release 里的打包版。

打包（可选）：

```bash
pip install pyinstaller
pyinstaller --noconfirm --windowed --name TeaEggPet ^
  --collect-all imageio_ffmpeg --collect-all certifi --collect-all tzdata ^
  --collect-all psutil --collect-all keyring --collect-all win32ctypes ^
  --collect-all PySide6.QtMultimedia --collect-all edge_tts --collect-all aiofiles ^
  --collect-all aiohttp --collect-all tabulate ^
  --add-data "assets;assets" --add-data "pet/menu_templates;pet/menu_templates" ^
  --add-data "pet/persona_presets;pet/persona_presets" ^
  --add-data "pet/companion/prompts;pet/companion/prompts" ^
  --add-data "LICENSE;." --add-data "THIRD_PARTY_NOTICES.md;." packaging/pet_entry.py
```

---

## 数据与隐私

- 所有数据都在本机 `%APPDATA%\dsh-pet-standalone`（配置、日程、素材库、照片、日志）。
- API Key 只存在你本机的配置里，不会上传任何服务器；换机器请自己重新填。
- 本仓库不含任何账号、Key 或个人素材。

---

## 目录结构（新增部分）

```
pet/companion/
  store.py            # 陪伴扩展自己的配置读写（companion_* 键）
  llm.py              # 复用宿主的 AI 设置
  presets.py          # 关键词（情境/神态/动作/镜头/光线）
  library.py          # 素材库扫描与写入（人物/服饰/场景/道具）
  compose.py          # 参考图组合 + 提示词拼装
  imagegen.py         # 生图入口（65535 / OpenAI 兼容 / ComfyUI）
  comfy.py            # 本地 ComfyUI：安装目录、模型扫描、启动、工作流、错误翻译
  qwen_prompt.py      # 千问编辑模板驱动的提示词重写
  schedule.py         # 每日日程
  selfie.py           # 自拍流程（选素材 → 重写提示词 → 生图 → 落盘）
  walk.py             # 出去溜达（QThread 服务 + 微信式汇报）
  photo.py            # 自拍照片卡片
  schedule_table.py   # 日程 × 素材联动表
  library_dialog.py   # 素材库与关键词管理窗口
  settings_dialog.py  # 陪伴扩展设置（生图接口 / 日程 / 溜达 / 说明）
  menu.py             # 右键菜单入口
  runtime.py          # 挂到宿主的启动逻辑（热键、单例窗口、宿主查找）
  prompts/            # 千问编辑模板
```

宿主的接入点：`pet/hotkeys.py`（`go_walk` / `selfie_now`）、`pet/context_menus/registry.py`
（`go_walk` / `selfie` / `companion`）、`pet/menu_templates/modern-default-v1.json`、
`pet/config.py`（热键白名单与配置重载）、`pet/app.py`（快捷键回调 + `runtime.attach`）。

---

## 许可

MIT，见 [LICENSE](LICENSE)。本仓库是 dsh-pet-indesktop（© Merzlin，MIT）的衍生作品，
保留原版权与许可；第三方组件许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
生图模型 Qwen-Image 2.1 与 ComfyUI 遵循其各自许可。
