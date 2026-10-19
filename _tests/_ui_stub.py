"""一次性 UI 桩：用假数据喂真实前端，只为在浏览器里看渲染。用完删掉。

真实 app.js / index.html / style.css，只是 API 返回假数据。
"""

import json
import os
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))   # 本脚本所在目录（_tests）
ARCH = os.path.dirname(HERE)                        # 生产代码所在目录
WEB = os.path.join(ARCH, "web")
CHAT = -1001234567890
PORT = int(os.environ.get("UI_PORT", "8793"))
GATE = os.environ.get("UI_GATE", "ok")   # 设成 setup 就能看首屏表单（假数据，不连网）

LIB = os.path.join(HERE, "_ui_stub_tmp")     # 放工作目录里，别丢到系统临时文件夹
os.makedirs(LIB, exist_ok=True)
TH = os.path.join(LIB, str(CHAT), "thumbs")
os.makedirs(TH, exist_ok=True)
for name, color, size in [("a.jpg", (198, 92, 84), (400, 400)),
                          ("b.jpg", (86, 138, 196), (400, 400)),
                          ("c.jpg", (110, 178, 122), (800, 600))]:
    im = Image.new("RGB", size, color)
    ImageDraw.Draw(im).text((24, 24), f"{name}  {size[0]}x{size[1]}", fill=(255, 255, 255))
    im.save(os.path.join(TH, name), quality=88)

now = int(time.time())
GROUPS = [
    {"key": "msg:1", "grouped_id": None, "date": now - 60, "kind": "photo",
     "kinds": ["photo"], "status": "done", "note": None, "size": 63829,
     "edited": now - 30, "deleted": None, "star": 0, "dups": [],
     "caption": "腿控日常", "author_id": 5352610642, "author_name": "夜",
     "topic_id": 654278, "topic_title": "腿控@日常拍腿",
     "permalink": "https://t.me/c/1234567890/654278/684210",
     "members": [{"msg_id": 684210, "kind": "photo", "thumb": "thumbs/a.jpg",
                  "file": "thumbs/c.jpg", "width": 800, "height": 600, "size": 63829,
                  "abs": r"D:\演示\library\-1001234567890\thumbs\c.jpg",
                  "status": "done", "note": None, "permalink": "x"}]},
    {"key": "album:999", "grouped_id": 999, "date": now - 120, "kind": "album",
     "kinds": ["photo", "video"], "status": "done", "note": None, "size": 90000,
     "edited": 0, "deleted": None, "star": now - 10,
     "caption": "黑丝榨精腿（相册：图+视频混合）", "author_id": 6427301876,
     "author_name": "阿泽", "topic_id": 654278, "topic_title": "腿控@日常拍腿",
     "permalink": "https://t.me/c/1234567890/654278/654373",
     "members": [{"msg_id": 654373, "kind": "photo", "thumb": "thumbs/b.jpg",
                  "file": "thumbs/c.jpg", "width": 800, "height": 600, "size": 30000,
                  "status": "done", "note": None, "permalink": "x"},
                 {"msg_id": 654374, "kind": "video", "thumb": "thumbs/a.jpg",
                  "file": "thumbs/c.jpg", "width": 720, "height": 1280, "size": 60000,
                  "status": "done", "note": None, "permalink": "x"}]},
    {"key": "msg:4", "grouped_id": None, "date": now - 300, "kind": "video",
     "kinds": ["video"], "status": "skipped", "note": "超过 100 MB", "size": 720600622,
     "edited": 0, "deleted": now - 20, "star": 0,
     "caption": "", "author_id": 5063266190, "author_name": None, "topic_id": None,
     "topic_title": None, "permalink": "https://t.me/c/1234567890/684051",
     "members": [{"msg_id": 684051, "kind": "video", "thumb": None, "file": None,
                  "width": 1920, "height": 1080, "size": 720600622, "status": "skipped",
                  "note": "超过 100 MB", "permalink": "x"}]},
]
AUTHORS = [{"author_id": 5352610642, "name": "夜", "n": 12, "bytes": 63829},
           {"author_id": 6427301876, "name": "阿泽", "n": 4, "bytes": 90000},
           {"author_id": 5063266190, "name": "", "n": 1, "bytes": 0}]
CHATS = {"chats": [
    {"chat_id": CHAT, "title": "测试群", "kind": "群", "noforwards": True,
     "forum": True, "watch": {"enabled": True, "kinds": ["photo", "video"], "max_mb": 100,
                              "started_at": now}},
    {"chat_id": -1009999999999, "title": "另一个群（没在监听）", "kind": "群",
     "noforwards": False, "forum": False, "watch": None}]}
STATIC = {"index.html": "text/html; charset=utf-8",
          "app.js": "application/javascript; charset=utf-8",
          "style.css": "text/css; charset=utf-8"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _j(self, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            u = urlparse("/index.html")
        name = u.path.lstrip("/")
        if name in STATIC:
            b = open(os.path.join(WEB, name), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", STATIC[name])
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif u.path == "/api/status":
            if GATE == "ok":
                self._j({"state": "ok", "error": None, "connected": True, "note": "",
                         "has_config": True, "has_session": True, "api_id": 12345678,
                         "api_hash_set": True, "local": True,
                         "locked": False, "passcode_set": False, "legacy_file": False})
            else:              # 造出「本地有 session、但还没填 api_id」这个状态
                self._j({"state": GATE, "error": None, "connected": False, "note": "",
                         "has_config": False, "has_session": True, "api_id": None,
                         "api_hash_set": False, "local": True,
                         "locked": GATE == "locked", "passcode_set": GATE == "locked",
                         "legacy_file": False})
        elif u.path == "/api/chats":
            self._j(CHATS)
        elif u.path == "/api/items":
            self._j({"groups": GROUPS, "used": 180000})
        elif u.path == "/api/authors":
            self._j({"authors": AUTHORS})
        elif u.path == "/api/stats":
            self._j({"used": 180000, "cap": 53687091200, "counts": {"done": 3},
                     "over_cap": False, "sweeps": {}, "note": ""})
        elif u.path.startswith("/media/"):
            p = os.path.join(LIB, u.path[7:].replace("/", os.sep))
            if os.path.isfile(p):
                b = open(p, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/backfill":
            return self._j({"added": 12})
        if p == "/api/delete":
            return self._j({"deleted": 1, "bytes": 30000})
        if p in ("/api/reveal", "/api/open"):
            return self._j({"ok": True, "path": r"D:\假路径\演示用.jpg"})
        self._j({"watch": {"chat_id": CHAT, "kinds": "photo,video", "max_mb": 100,
                           "enabled": 1, "started_at": now}})


print(f"UI 桩：http://127.0.0.1:{PORT}/   图片在 {TH}", flush=True)
ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
