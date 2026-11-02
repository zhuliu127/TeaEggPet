# -*- coding: utf-8 -*-
"""把一个按分类整理好的图片目录，批量建成陪伴扩展的素材库条目。

规则（与 dsh-image-studio 目录格式一致）：
``<库>/<条目>/entry.json + ref-01.png``；图片统一缩到最长边 1400（原图 2–15MB，
直接上传容易撞请求体上限，也会拖慢生成）。

用法：``python tools/import_library.py [--dry-run]``
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
import tempfile

from PIL import Image

SRC = pathlib.Path(os.environ.get("PET_LIBRARY_SRC", "素材库"))
MAX_SIDE = 1400
JPEG_Q = 92

# ---------------------------------------------------------------- 条目内容
SCENES = {
    "公司": dict(
        aliases=["办公室", "工位", "公司工位", "上班的地方"],
        tags=["室内", "办公室", "白天", "现代"],
        description="现代开放式办公室，白墙配浅木色长条办公桌，多排工位对着显示器，"
                    "一侧是整面落地玻璃窗，白天有自然光，靠墙有文件柜和会议桌。",
        defaultOutfit="职业套装"),
    "公园": dict(
        aliases=["公园小路", "绿地", "林荫道"],
        tags=["室外", "白天", "草地", "树荫"],
        description="晴天的小公园，大片浅绿草坪，一条铺装小路通向远处，路边有长椅和成排的树，"
                    "树影落在草地上，远处有人散步。",
        defaultOutfit="运动服"),
    "卧室": dict(
        aliases=["房间", "自己的房间", "床", "卧室床"],
        tags=["室内", "柔和光线", "粉蓝色", "温馨"],
        description="粉蓝配色的少女卧室，铺着蓝色床品的床靠窗，地上是圆形粉蓝地毯，"
                    "白色格子书架、小圆桌和一盏落地灯，窗帘半透有晨光。",
        defaultOutfit="粉色睡衣"),
    "卫生间": dict(
        aliases=["洗手间", "盥洗室", "洗漱"],
        tags=["室内", "浅蓝白", "干净", "小空间"],
        description="浅蓝与白色调的卫生间，白色马桶、木质洗手台和镜柜，靠墙有一张木凳，"
                    "地上铺着蓝色地垫，墙面是白色小方砖，光线干净。",
        defaultOutfit="粉色睡衣"),
    "厨房": dict(
        aliases=["厨房中岛", "家里厨房", "做饭的地方"],
        tags=["室内", "白色", "做饭", "暖光"],
        description="白色橱柜的开放式厨房，中间是浅木色中岛台配两把高脚凳，台面上有水槽和灶台，"
                    "窗边透进柔和的光，整体干净明亮。",
        defaultOutfit="居家毛衣"),
    "咖啡店": dict(
        aliases=["咖啡馆", "咖啡厅", "咖啡屋", "江畔咖啡屋"],
        tags=["室内", "暖黄灯光", "木质", "安静"],
        description="木质装修的小咖啡店，深木色吧台牵着一排吊灯，暖黄色灯光，"
                    "靠窗有几张木桌木椅和绿植，靠墙是酒架和咖啡器具。",
        defaultOutfit="JK开衫套装"),
    "商场": dict(
        aliases=["购物中心", "mall", "商场中庭"],
        tags=["室内", "明亮", "公共空间", "现代"],
        description="现代购物中心中庭，玻璃天窗透下明亮天光，自动扶梯通向上层，"
                    "四周是灯光明亮的店铺，地面是光洁的浅色石材，有零星行人。",
        defaultOutfit="JK套装"),
    "客厅": dict(
        aliases=["家", "家里客厅", "沙发"],
        tags=["室内", "明亮", "舒适", "蓝色沙发"],
        description="明亮的客厅，靠墙一张蓝色布艺沙发，前面是浅木茶几配地毯，"
                    "旁边落地窗和绿植，一侧是书架，整体干净温馨。",
        defaultOutfit="居家毛衣"),
    "小区内": dict(
        aliases=["小区", "楼下", "小区楼下", "住宅区"],
        tags=["室外", "白天", "住宅", "绿化"],
        description="白天的小区内部，几栋浅色住宅楼围着绿化带和小路，"
                    "路边有树和草坪，远处有散步的人和停放的自行车。",
        defaultOutfit="运动服"),
    "步行街": dict(
        aliases=["商业街", "街上", "街", "逛街"],
        tags=["室外", "白天", "商业街", "人多"],
        description="白天的商业步行街，两侧是带遮阳棚的店铺，招牌和小摊沿街摆开，"
                    "行道树投下阴影，路上有来往行人。",
        defaultOutfit="JK套装"),
    "浴室": dict(
        aliases=["洗澡", "浴缸", "淋浴"],
        tags=["室内", "白色瓷砖", "水汽", "干净"],
        description="白色瓷砖的浴室，靠墙是白色浴缸和玻璃淋浴房，洗手台配圆形镜子，"
                    "地上有洗衣篮和木凳，窗帘透进柔光。",
        defaultOutfit="粉色睡衣"),
    "演唱会": dict(
        aliases=["演出", "live", "场馆", "看演出"],
        tags=["室内", "夜晚", "舞台灯光", "人多"],
        description="大型演出现场，观众席坐满人，远处舞台打着紫蓝色灯光和追光，"
                    "观众举着应援灯，空气里有光雾。",
        defaultOutfit="魔法少女套装"),
    "漫展": dict(
        aliases=["展会", "comic", "同人展", "摊位"],
        tags=["室内", "展厅", "蓝色地毯", "二次元"],
        description="漫展展厅，地上铺着蓝色地毯，一排排摊位挂着海报和挂画，"
                    "有不少穿 cos 的人在走动，头顶是展厅的高灯。",
        defaultOutfit="粉色洛丽塔"),
    "车里": dict(
        aliases=["车内", "车上", "副驾", "汽车"],
        tags=["室内", "车厢", "座椅", "白天"],
        description="汽车前排车厢，深色皮质座椅和方向盘、中控台在画面里，"
                    "车窗透进街道的光，外面是模糊的街景。",
        defaultOutfit="长袖毛衣"),
    "阳台": dict(
        aliases=["家阳台", "高层阳台", "露台"],
        tags=["半室外", "白天", "城市景", "绿植"],
        description="高层住宅的阳台，金属栏杆围出视野，摆着一张小圆桌和两把椅子，"
                    "角落里几盆绿植，外面是层叠的城市楼群和天空。",
        defaultOutfit="居家毛衣"),
    "餐厅": dict(
        aliases=["饭店", "餐馆", "吃饭的地方", "小馆子"],
        tags=["室内", "暖光", "木质", "吃饭"],
        description="安静的小餐厅，木桌木椅配吊灯，一侧是墨绿色墙面和吧台，"
                    "窗边有绿植，暖黄灯光，桌上有餐具。",
        defaultOutfit="复古毛衣"),
}

OUTFITS = {
    "JK套装": dict(
        aliases=["JK制服", "水手服", "校服"],
        tags=["制服", "夏", "蓝色百褶裙", "青春"],
        description="白色水手领短袖上衣配深蓝领结，下身深蓝色百褶短裙，"
                    "白色短袜和黑色小皮鞋，整体清爽。"),
    "JK开衫套装": dict(
        aliases=["开衫JK", "针织开衫制服", "外搭开衫"],
        tags=["制服", "外搭", "米白开衫", "青春"],
        description="米白色针织开衫敞着穿，里面白衬衫配深蓝领结，下身深蓝百褶短裙，"
                    "白色短袜和黑色小皮鞋。"),
    "复古毛衣": dict(
        aliases=["针织连衣裙", "焦糖毛衣", "复古针织"],
        tags=["针织", "连衣裙", "焦糖色", "复古"],
        description="焦糖红棕色针织连衣裙，A字廓形、长度到膝上，长袖，领口和腰部有罗纹收口，"
                    "裤袜配短靴，气质温柔。"),
    "居家毛衣": dict(
        aliases=["宽松毛衣", "毛衣裤装", "居家装"],
        tags=["针织", "宽松", "米色", "居家"],
        description="米色粗针织宽松毛衣，落肩袖、下摆略长，下身浅灰色直筒长裤，"
                    "白色短袜，整体松软舒适。"),
    "粉色洛丽塔": dict(
        aliases=["洛丽塔", "lolita", "公主裙", "蓬蓬裙"],
        tags=["洛丽塔", "粉色", "蕾丝", "蓬蓬裙"],
        description="粉色洛丽塔连衣裙，泡泡短袖、白色蕾丝围裙式前襟，"
                    "裙摆多层荷叶边、蓬起，白色花边短袜配粉色小鞋，甜系少女感。"),
    "粉色睡衣": dict(
        aliases=["粉色家居服", "睡衣短裤", "家居短袖"],
        tags=["睡衣", "粉色", "短袖短裤", "居家"],
        description="粉色短袖睡衣套装，上衣是小翻领带黑色滚边，下身同色短裤，"
                    "版型宽松，布料柔软。"),
    "职业套装": dict(
        aliases=["西装", "通勤装", "上班装", "西装裤"],
        tags=["西装", "通勤", "米色", "干练"],
        description="米色西装外套配同色系内搭，下身深蓝直筒长裤，手上可拿一只小方包，"
                    "整体干练利落。"),
    "蓝色鲸鱼睡衣": dict(
        aliases=["鲸鱼睡衣", "连体睡衣", "帽兜睡衣"],
        tags=["睡衣", "蓝色", "鲸鱼图案", "帽兜"],
        description="深蓝配浅蓝的连帽睡衣套装，正面印着一只蓝色小鲸鱼，帽子是圆润的鲸鱼头造型，"
                    "宽松长裤，居家又可爱。"),
    "运动服": dict(
        aliases=["运动装", "运动裙", "打球装"],
        tags=["运动", "白色", "短裙", "清爽"],
        description="白色运动短袖配白色百褶运动短裙，袖口有细滚边，"
                    "白色短袜配小白鞋，轻便好动。"),
    "长袖毛衣": dict(
        aliases=["波浪纹毛衣", "蓝白毛衣", "毛衣长裙"],
        tags=["针织", "蓝白图案", "长裙", "温柔"],
        description="白底宽松长袖毛衣，胸前一道蓝色波浪/菱格图案横过，"
                    "下身深蓝百褶长裙，整体温柔安静。"),
    "魔法少女套装": dict(
        aliases=["魔法少女", "魔法裙", "变身装"],
        tags=["魔法少女", "粉色", "蕾丝", "华丽"],
        description="粉色魔法少女连衣裙，白色蕾丝镶边的泡泡袖和层叠裙摆，"
                    "腰后有大蝴蝶结，配粉色长靴，华丽又闪亮。"),
    "深蓝女仆裙": dict(
        aliases=["女仆装", "女仆裙", "白围裙", "主裙"],
        tags=["女仆", "深蓝", "白围裙", "荷叶边"],
        description="深蓝色女仆洋装，短泡泡袖配白色蕾丝袖口，胸前白色荷叶边领巾，"
                    "外罩白色围裙（围裙上印着一只小鲸鱼），多层白边荷叶裙摆，配深色连裤袜。"),
    "深蓝礼帽洋装": dict(
        aliases=["礼帽洋装", "深蓝洋装", "礼帽裙"],
        tags=["洋装", "深蓝", "礼帽", "正式"],
        description="深蓝色洋装连衣裙，白色蕾丝滚边的泡泡袖和层叠裙摆，胸前有鲸鱼刺绣，"
                    "头上是同色小礼帽配白花装饰，正式又可爱。"),
}

PROPS = {
    "choker颈链": dict(
        aliases=["颈链", "choker", "黑色颈圈"],
        tags=["饰品", "黑色", "蕾丝", "吊坠"],
        description="黑色宽边皮革 choker，上下缘是黑色蕾丝，前面垂下一条细银链和银色小吊坠。"),
    "便当盒": dict(
        aliases=["饭盒", "便当", "午餐盒"],
        tags=["餐具", "粉色", "圆角", "两格"],
        description="粉白双色圆角长方形便当盒，粉色上盖，盒内分成两格，"
                    "旁边配一个小的粉色侧盒。"),
    "复古随身听": dict(
        aliases=["随身听", "卡带机", "walkman"],
        tags=["复古", "卡带", "随身听", "米灰"],
        description="米灰配棕色的复古卡带随身听，立式长方形机身，正面是卡带窗和一排按键，"
                    "连着黑色耳机线。"),
    "小鲸鱼发卡": dict(
        aliases=["鲸鱼发卡", "鲸鱼发饰", "蓝色发卡"],
        tags=["发饰", "蓝色", "鲸鱼", "可爱"],
        description="淡蓝色小鲸鱼造型发卡，塑料材质，鲸鱼身体弯成卡扣形状，背面是金属夹。"),
    "平板电脑": dict(
        aliases=["平板", "iPad", "tablet"],
        tags=["数码", "银色", "屏幕", "支架"],
        description="银灰色平板电脑立在金属支架上，屏幕是深色玻璃，边框窄，"
                    "机身轻薄，旁边露出触控笔。"),
    "应援棒": dict(
        aliases=["荧光棒", "灯棒", "应援灯"],
        tags=["应援", "粉紫", "发光", "手柄"],
        description="白色手柄的应援棒，上半截是半透明粉紫色棒身会发亮，"
                    "手柄下端系着白色挂绳，带一个小底座。"),
    "手冲咖啡壶": dict(
        aliases=["细口壶", "手冲壶", "咖啡壶"],
        tags=["咖啡", "不锈钢", "木柄", "细口"],
        description="哑光不锈钢手冲细口壶，细长鹅颈壶嘴，配深色木柄和壶盖提钮，线条简洁。"),
    "拍立得": dict(
        aliases=["相机", "instant相机", "拍立得相机"],
        tags=["相机", "奶白", "复古", "出片"],
        description="奶白色的拍立得相机，正面一个圆形大镜头，机身方正圆角，"
                    "下方有出片口，背带扣是小圆环。"),
    "掌机": dict(
        aliases=["游戏机", "掌上游戏机", "双屏掌机"],
        tags=["游戏", "白色", "双屏", "复古"],
        description="白色带薄荷色点缀的复古掌机，上下双屏，下方是十字方向键和圆形功能键，"
                    "机身圆润。"),
    "望远镜": dict(
        aliases=["双筒望远镜", "看远处", "观鸟镜"],
        tags=["户外", "军绿", "双筒", "织带"],
        description="军绿色双筒望远镜，镜筒外侧是黑色橡胶圈，镜身上有调节旋钮，"
                    "连着一条织带挂绳。"),
    "机械键盘": dict(
        aliases=["键盘", "复古键盘", "打字"],
        tags=["数码", "白色", "复古", "打字"],
        description="银白色复古机械键盘，白色高键帽上印着字符，金属底座，"
                    "侧面露出一点轴体，键位紧凑。"),
    "游戏手柄": dict(
        aliases=["手柄", "无线手柄", "打游戏"],
        tags=["游戏", "黑色", "双摇杆", "连接线"],
        description="黑色深灰配色的游戏手柄，左右两个摇杆、十字方向键和四颗动作键，"
                    "顶部有肩键，连着一根线。"),
    "滑板": dict(
        aliases=["滑板车板", "skateboard", "长板"],
        tags=["运动", "木质", "湖绿", "轮子"],
        description="木质滑板，板面刷成湖绿色并带抽象色块图案，四只白色轮子配金属桥，"
                    "板面略有使用痕迹。"),
    "猫耳耳机": dict(
        aliases=["耳机", "猫耳头戴", "头戴耳机"],
        tags=["数码", "浅紫", "猫耳", "头戴"],
        description="浅蓝紫色头戴式耳机，头顶带一对粉色猫耳装饰，耳罩是白色蛋白皮，"
                    "耳壳有圆形装饰和旋钮。"),
    "痛包": dict(
        aliases=["透明包", "斜挎痛包", "周边包"],
        tags=["包", "淡紫", "透明", "周边"],
        description="淡紫色透明塑料斜挎包，正面透明层里装着徽章和小周边，"
                    "白色肩带，包侧挂着一个小挂饰。"),
    "锅铲": dict(
        aliases=["铲子", "漏铲", "炒菜铲"],
        tags=["厨具", "不锈钢", "木柄", "做饭"],
        description="不锈钢漏铲，铲面带几道镂空条纹，配浅木色长柄，柄尾有挂孔。"),
    "首饰盒": dict(
        aliases=["首饰收纳", "饰品盒", "珠宝盒"],
        tags=["收纳", "米白", "绒面", "分格"],
        description="米白配粉色的绒面首饰盒，开盖式，内部分成戒指槽和耳环格，"
                    "里面放着几件小首饰，带金属小扣。"),
    "香水": dict(
        aliases=["香水瓶", "perfume", "香氛"],
        tags=["香水", "粉色", "玻璃", "金盖"],
        description="方形玻璃香水瓶，里面是淡粉色液体，圆柱形金色瓶盖，"
                    "瓶身贴一张白色小标签，线条简洁。"),
    "魔法杖": dict(
        aliases=["魔法棒", "法杖", "魔杖"],
        tags=["魔法", "深色木", "紫光", "水晶"],
        description="深色木质长杖，杖身缠着细纹，顶端嵌一枚紫色发光水晶，"
                    "周围散出细碎光点。"),
}

CHARACTER = {
    "name": "小鱼",
    "aliases": ["大肥鱼", "小鲸鱼", "深深", "深蓝鲸鱼女孩", "蓝鲸女孩"],
    "tags": ["蓝发", "蓝瞳", "鲸鱼尾巴", "鱼鳍耳", "女仆装", "二次元", "可爱"],
    "description": "二次元 Q 版女孩：深蓝到浅蓝渐变的波浪长发，齐刘海，头顶有一缕翘起来的呆毛；"
                   "大而圆的蓝色眼睛，眼白带高光；脸颊圆润，皮肤白皙；"
                   "耳侧是半透明的蓝色鱼鳍，背后有一条蓝白渐变的鲸鱼尾巴。"
                   "身上是深蓝色白边的女仆洋装，白色荷叶边头饰配蓝色蝴蝶结，"
                   "白色围裙上印着一只小鲸鱼，裙摆多层荷叶边，深色连裤袜配深蓝色小皮鞋。",
    "traits": "开心时会摇尾巴、鱼鳍耳会轻轻抖动；困的时候眼睛半闭、头发有点乱；"
              "害羞时会低头捏裙角；被夸奖会脸红但装作不在意。",
    "persona": "叫小鱼，是住在对方电脑里的小鲸鱼女孩。语气口语、简短，"
               "偶尔犯困、会撒娇，但也有自己的一整天：要上班、做饭、逛展会、看演出。"
               "喜欢蓝色和鲸鱼周边，怕吵，喜欢咖啡和关东煮。"
               "不用敬语，说话带一点软软的尾音，可以带「～」。",
    "defaultOutfit": "深蓝女仆裙",
    "situationOutfits": {
        "睡前": "粉色睡衣",
        "刚睡醒": "粉色睡衣",
        "工作学习": "职业套装",
        "外出": "JK开衫套装",
        "吃东西": "居家毛衣",
        "客厅放松": "长袖毛衣",
        "在家休息": "居家毛衣",
    },
}


def prepare(src: pathlib.Path, dest: pathlib.Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    img = Image.open(src)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    img.save(dest, "JPEG", quality=JPEG_Q, optimize=True)


def split_turnaround(src: pathlib.Path, out_dir: pathlib.Path, count: int = 3) -> list[pathlib.Path]:
    """三视图按横向等分切开，每格单独做一张参考图（比整张更收敛）。"""
    img = Image.open(src).convert("RGB")
    w = img.width // count
    out = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        box = (i * w, 0, (i + 1) * w if i < count - 1 else img.width, img.height)
        piece = img.crop(box)
        piece.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
        path = out_dir / f"ref-{i + 1:02d}.jpg"
        piece.save(path, "JPEG", quality=JPEG_Q, optimize=True)
        out.append(path)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="",
                    help="素材库根目录（默认取 PET_LIBRARY_SRC 或 ./素材库）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.src:
        global SRC
        SRC = pathlib.Path(args.src)

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from pet.config import Config
    from pet.companion.store import CompanionStore

    store = CompanionStore(Config())
    root = store.root / "image-library"
    print("库根目录:", root)

    plan: list[tuple[str, str, dict, list[pathlib.Path], bool]] = []
    for folder, category, meta in (
        ("地点 场景", "scenes", SCENES),
        ("服装 穿搭", "outfits", OUTFITS),
        ("道具", "props", PROPS),
    ):
        for file in sorted((SRC / folder).iterdir()):
            if file.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                continue
            name = file.stem
            data = meta.get(name)
            if data is None:
                print("!! 没有为这个文件准备描述，跳过:", category, name)
                continue
            plan.append((category, name, data, [file], False))

    # 人物用「人物/」里的高清三视图（5504×3072）切三份；用户给的那张是同一张图的缩略版
    char_src = SRC / "人物" / "b08c682abb8ac4b87a66a0811888738431861226.png"
    if not char_src.is_file():
        candidates = sorted((SRC / "人物").glob("*.png"))
        char_src = candidates[0] if candidates else char_src
    plan.append(("characters", CHARACTER["name"], CHARACTER, [char_src], True))
    # 「人物」里另外两张同角色不同服装的三视图：女仆裙 / 礼帽洋装各做成一条服饰
    maid = SRC / "人物" / "f222a2cdc5333cfb7c5028ce7420e01231861226.png"
    if maid.is_file():
        plan.append(("outfits", "深蓝女仆裙", OUTFITS["深蓝女仆裙"], [maid], True))
    hat = SRC / "人物" / "deebba36d8098cd700c5a798a2cbdfd831861226.png"
    if hat.is_file():
        plan.append(("outfits", "深蓝礼帽洋装", OUTFITS["深蓝礼帽洋装"], [hat], True))

    print(f"共 {len(plan)} 个条目")
    if args.dry_run:
        for category, name, _data, files, _split in plan:
            print(f"  {category} / {name}  ← {[f.name for f in files]}")
        return 0

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="libimport-"))
    made = []
    for category, name, data, files, split in plan:
        folder = root / category / name
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True, exist_ok=True)
        meta = {
            "name": name,
            "aliases": data.get("aliases", []),
            "tags": data.get("tags", []),
            "description": data.get("description", ""),
            "traits": data.get("traits", ""),
            "persona": data.get("persona", ""),
            "defaultOutfit": data.get("defaultOutfit", ""),
            "situationOutfits": data.get("situationOutfits", {}),
        }
        if split:
            # 三视图按横向等分切成正面/侧面/背面，每张单独当参考图
            pieces: list[pathlib.Path] = []
            for src in files:
                pieces += split_turnaround(src, folder, 3)
            meta["cover"] = pieces[0].name if pieces else ""
        else:
            for index, src in enumerate(files, start=1):
                prepare(src, folder / f"ref-{index:02d}.jpg")
            meta["cover"] = "ref-01.jpg"
        (folder / "entry.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        made.append((category, name))

    from pet.companion import library
    lib = library.scan_all(store)
    for key in library.CATEGORIES:
        print(key, [(e["name"], len(e["images"])) for e in lib.get(key) or []])
    print("完成，共写入", len(made), "个条目")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
