# -*- coding: utf-8 -*-
"""本地假接口（自测用）：chat/completions、65535 异步任务、OpenAI images、图片下载。"""
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw

SCHEDULE = {
    "title": "请了半天假，下午去河边走了很久",
    "mood": "松弛，有点想见你",
    "items": [
        {"start": "00:00", "end": "08:10", "activity": "睡觉", "location": "家", "detail": ""},
        {"start": "08:10", "end": "09:00", "activity": "赖床刷手机", "location": "家-卧室", "detail": "被子裹成一团"},
        {"start": "09:00", "end": "10:30", "activity": "煮面当早午饭", "location": "家-厨房", "detail": "煎糊了一个蛋"},
        {"start": "10:30", "end": "12:00", "activity": "写代码", "location": "家-书房", "detail": "把脚本跑通了"},
        {"start": "12:00", "end": "13:30", "activity": "出门散步", "location": "河边", "detail": "风很大，围巾一直飞"},
        {"start": "13:30", "end": "15:00", "activity": "在咖啡屋坐着", "location": "江畔暖灯咖啡屋", "detail": "拿铁拉花是只小猫"},
        {"start": "15:00", "end": "17:30", "activity": "逛便利店", "location": "便利店", "detail": "买了关东煮"},
        {"start": "17:30", "end": "19:00", "activity": "做晚饭", "location": "家-厨房", "detail": "煮了味噌汤"},
        {"start": "19:00", "end": "23:20", "activity": "打游戏", "location": "家-客厅", "detail": "连输三把"},
        {"start": "23:20", "end": "24:00", "activity": "睡觉", "location": "家", "detail": ""},
    ],
}

PLAN = {
    "caption": "河边的风好大，头发都飞起来啦～",
    "monologue": "我回来啦，给你看照片！",
    "prompt": "傍晚的河边步道，风把她的头发吹向一侧，一手举着手机自拍，另一只手压着裙摆，身后是橙色夕阳",
    "situation": "外出", "expression": "开心", "pose": "站着", "shot": "自拍", "light": "黄昏逆光",
    "characters": ["深深"], "outfits": [], "scene": "", "props": [], "aspect": "3:4",
}


def _png() -> bytes:
    img = Image.new("RGB", (768, 1024))
    draw = ImageDraw.Draw(img)
    for y in range(1024):
        draw.line([(0, y), (768, y)], fill=(120 + y // 12, 150 + y // 20, 220 - y // 16))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


PNG = _png()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/shot.png"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
            return
        if "/tasks/" in self.path:
            time.sleep(0.8)
            return self._json({"status": "done", "model": "mock-image",
                               "result_urls": [f"http://127.0.0.1:8899/shot.png"]})
        if self.path.rstrip("/").endswith("/models"):
            return self._json({"data": [{"id": "mock-chat"}, {"id": "mock-image"}]})
        return self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if self.path.endswith("/chat/completions"):
            try:
                payload = json.loads(raw.decode("utf-8"))
            except ValueError:
                payload = {}
            text = "\n".join(str(m.get("content", "")) for m in payload.get("messages", []))
            if "自拍" in text:
                content = json.dumps(PLAN, ensure_ascii=False)
            elif "日程" in text:
                content = json.dumps(SCHEDULE, ensure_ascii=False)
            else:
                content = "我在呢～刚泡了杯茶，你呢？"
            return self._json({"choices": [{"message": {"role": "assistant", "content": content}}]})
        if self.path.endswith("/tasks"):
            # 模拟「这个分组不在这台网关」：让客户端走通道自动回退
            if os.environ.get("MOCK_TASKS_FORBIDDEN") == "1":
                return self._json({"error": {"message": "this group is not served by this gateway",
                                             "type": "Forbidden"}}, 403)
            return self._json({"id": f"task-{int(time.time())}"})
        if "/images/" in self.path:
            return self._json({"data": [{"url": "http://127.0.0.1:8899/shot.png"}]})
        return self._json({"error": {"message": "not found"}}, 404)


def main(port: int = 8899) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"mock api on http://127.0.0.1:{port}/v1", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
