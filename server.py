"""本地网页服务：扫码登录、群列表、监听开关、实时条目、媒体文件。

只绑 0.0.0.0（本机和手机都能连，所以别把库目录放到别人也看得到的地方）。
密码门在 Handler._authed()：设过密码就要输，没设就直接进。
Telethon 跑在另一个线程的 asyncio loop 里，HTTP 处理通过
run_coroutine_threadsafe 过桥 —— 所以库里的一切读写都发生在 loop 线程上，
不需要加锁（sqlite 连接不能跨线程用）。

媒体必须由这里发出去（而不是让页面用 file:// 相对路径）：浏览器会禁止
http 页面加载 file:// 的图片。视频要能拖进度条，所以支持 Range。
"""

import argparse
import asyncio
import csv
import ctypes
import hashlib
import hmac
import io
import json
import mimetypes
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

# 客户端中途挂断（手机拖视频、切页、锁屏）会抛这几类异常。它们是正常现象，
# 不是错误 —— 不接住的话每个断开的请求都会往 _run.log 里写一整段 traceback。
CONN_GONE = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)

import tg
import store
import gallery
import watcher as watcher_mod

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
LOG_NAME = "_run.log"
HB_LOG_NAME = "_hb.log"     # 临时侦查日志：端口心跳，定位完连线程带文件一起删
PID_NAME = "_server.pid"
STATIC = {"index.html": "text/html; charset=utf-8",
          "app.js": "application/javascript; charset=utf-8",
          "style.css": "text/css; charset=utf-8"}
# 设了口令时只放行这几个：解锁/设口令本身，加上状态查询。
# 静态文件也要放行，不然锁屏都画不出来。
LOCK_LOCAL = ("/api/unlock", "/api/passcode", "/api/session/reset")
LOCKED_OK = LOCK_LOCAL + ("/api/status", "/api/login")


def _host_ips():
    """本机自己的网卡地址。

    用局域网 IP 打开 PC 页面时客户端地址不是回环，但那仍然是本机，
    「在资源管理器中显示」不该莫名消失。
    """
    out = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            out.add(info[4][0].split("%")[0])
    except OSError:
        pass
    return out


_HOST_IPS = _host_ips()

# ---------- 临时侦查仪器：心跳线程 ----------
# 「电脑长时间无操作后手机连不上」到底是系统把服务挂起了，还是网络层断了 ——
# 每 60 秒自检一次自己的端口和当前 LAN IP，带时间戳写进 _hb.log。
# 手机报错的时刻回头对时间轴：心跳断档 = 整个进程（多半含全系统）被停了；
# 心跳连续且 ip 没变、listen=ok = 应用和电脑都活着，责任在网络层（网卡/路由器/防火墙）。
# 定位完这一段 + _hb.log 文件一起删。
_LAST_HB = 0.0             # 上一行心跳的时刻，下一次写时用来看断档


def _hb_write(msg):
    """追加一行到 _hb.log（超过 5MB 就重头再写，不无限膨胀）。"""
    global _LAST_HB
    gap = int(time.time() - _LAST_HB)
    note = ""
    if _LAST_HB and gap > 90:
        note = f"　[注意：距上一行断档 {gap} 秒 —— 这段时间整个进程（多半含全系统）都停了]"
    try:
        path = os.path.join(HERE, HB_LOG_NAME)
        if os.path.exists(path) and os.path.getsize(path) > 5_000_000:
            open(path, "w", encoding="utf-8").close()
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg + note + "\n")
    except OSError:
        pass
    _LAST_HB = time.time()


def _hb_ip():
    """UDP connect 骗出来的本机出口 IP（不真正发包）。网卡断开/断网时拿不到，返回''。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 9))          # UDP connect 只选路由，不发包
        ip = s.getsockname()[0]
        s.close()
        return "" if ip.startswith("127.") else ip
    except OSError:
        return ""


def hb_loop(port):
    global _LAST_HB
    _LAST_HB = time.time()                 # 起跑线，免得第一行就误报「断档」
    prev_ip = None
    while True:
        try:
            s = socket.socket()
            s.settimeout(2)
            ok = s.connect_ex(("127.0.0.1", port)) == 0
            s.close()
            ip = _hb_ip()
            _hb_write(f"listen={'ok' if ok else 'FAIL'} ip={ip or '-'}")
            if not ok:
                _hb_write("端口自检失败：服务进程/线程大概率整个被挂起了")
            if prev_ip is not None and ip != prev_ip:
                _hb_write(f"IP 变了：{prev_ip or '-'} -> {ip or '-'}（DHCP/路由器侧）")
            prev_ip = ip
        except Exception as exc:
            _hb_write(f"hb 出错 {type(exc).__name__}: {exc}")
        time.sleep(60)

# 电源请求（Windows 的「System Required」）。这台机器只支持 Modern Standby
# （S0 低电量待机）：系统一进待机，Windows 的 DAM 会把桌面程序整个挂起，服务答不上
# 手机的请求 —— 手机看到浏览器错误页，在电脑上动一下鼠标就又好了。握着这个请求，
# 接着电源时系统不会进待机（屏幕照样可以黑），服务就一直能被手机连上。
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
AWAKE = False          # 主线程设上了没有；/api/status 如实报出去


def keep_awake(on=True):
    """握上/松开电源请求。返回 False 表示没设上。

    SetThreadExecutionState 的返回值是「调用前」的状态，而 0 也可能是成功，
    所以连着调两次：第二次返回的就是刚设上的标志位，非 0 才算真设上了。
    """
    flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED) if on else ES_CONTINUOUS
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:
        return False


def is_local(addr):
    """请求是不是来自本机。回环必然是；本机自己的网卡 IP 也算。"""
    host = (addr or "").split("%")[0]
    return host in ("127.0.0.1", "::1", "localhost") or host in _HOST_IPS


def _lan_ip():
    """挑一个能写进地址栏的局域网 IPv4。"""
    for ip in sorted(_HOST_IPS):
        if ip.count(".") == 3 and not ip.startswith("127."):
            return ip
    return "本机IP"


def hash_pw(password, salt=None):
    """密码只存 PBKDF2 加盐哈希，不存明文。返回 (salt, hash)。"""
    salt = salt or os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                            salt.encode("ascii"), 120000)
    return salt, h.hex()


def check_pw(password, salt, want):
    if not (salt and want):
        return False
    return hmac.compare_digest(hash_pw(password, salt)[1], want)


def show_in_explorer(path):
    """在资源管理器里选中这个文件。

    路径必须单独加引号：explorer 的 /select, 一旦没引号（路径里没空格时 list2cmdline
    就不加），它会解析失败、退化成打开「文档」文件夹。所以这里自己拼命令行。
    """
    subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')


LOGIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>归档</title>
<style>body{font:15px/1.6 "Segoe UI","Microsoft YaHei",system-ui;background:#12141a;
color:#e7e9ee;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
form{background:#1a1d24;border:1px solid #2a2f39;border-radius:14px;padding:26px;width:300px}
input{width:100%;padding:9px 11px;margin:10px 0;border-radius:8px;border:1px solid #2a2f39;
background:#14171d;color:inherit;box-sizing:border-box}
button{width:100%;padding:10px;border:0;border-radius:8px;background:#5b9dff;color:#0b0d10;
font-weight:600;cursor:pointer}
p{color:#98a1b0;font-size:13px}.e{color:#ef6b5e;font-size:13px}</style>
</head><body><form method="post" action="/api/login"><b>归档 · 需要密码</b>
<input type="password" name="pw" placeholder="访问密码" autofocus>
<button>进入</button><p>密码在电脑上的「设置」里设。这台机器上打开不用密码。</p>{err}
</form></body></html>"""


def safe_lib_path(root, rel):
    """URL 里的相对路径 -> 库目录里的真实路径。越界一律拒绝。"""
    rel = unquote(rel).replace("\\", "/").lstrip("/")
    if not rel or rel.startswith("..") or "/../" in rel or rel.endswith("/.."):
        return None
    path = os.path.abspath(os.path.join(root, rel.replace("/", os.sep)))
    root_abs = os.path.abspath(root)
    if path != root_abs and not path.startswith(root_abs + os.sep):
        return None
    return path


def parse_range(header, size):
    """解析 Range 头，返回 (start, end)，含头含尾。

    视频拖进度条靠它。看不懂或要不到就返回 None，调用方退回整个文件（200）。
    """
    if not header or not header.startswith("bytes=") or size <= 0:
        return None
    spec = header[6:].split(",")[0].strip()
    if "-" not in spec:
        return None
    a, _, b = spec.partition("-")
    try:
        if a == "":                                  # bytes=-500：最后 500 字节
            n = int(b)
            return (max(0, size - n), size - 1) if n > 0 else None
        start = int(a)
        end = int(b) if b else size - 1
    except ValueError:
        return None
    if start > end or start >= size:
        return None
    return start, min(end, size - 1)


class _Tee:
    """输出同时进控制台和日志文件。

    两个理由：
    1. run.bat 双击时窗口可能一闪就没了，至少还有 _run.log 可查；
    2. pythonw（无窗口模式）下 sys.stdout 是 None，不接管的话 print 会直接炸。
    """

    def __init__(self, path):
        self.fh = open(path, "a", encoding="utf-8", errors="replace")

    def write(self, s):
        try:
            sys.__stdout__.write(s)
        except Exception:
            pass
        self.fh.write(s)
        self.fh.flush()

    def flush(self):
        try:
            sys.__stdout__.flush()
        except Exception:
            pass
        self.fh.flush()


def install_log():
    """日志常开（而不是选项）：无窗口模式下它是唯一能看到输出的地方。"""
    path = os.path.join(HERE, LOG_NAME)
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 5 * 1024 * 1024:
            os.remove(path)
    except OSError:
        pass
    try:
        sys.stdout = sys.stderr = _Tee(path)
    except OSError:
        pass


class App:
    """asyncio loop 线程 + 生命周期。HTTP 线程只通过 call/call_fn 进来。"""

    def __init__(self, args):
        self.args = args
        self.loop = asyncio.new_event_loop()
        self.watcher = None
        self.error = None
        self.httpd = None            # 监听器由 serve() 建
        self.host = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        self.ready.wait(60)
        if self.error is not None:
            raise self.error
        # 后台自己连一次：有 session 就直接进主界面，别一上来就让人扫码
        asyncio.run_coroutine_threadsafe(self.watcher.boot(), self.loop)
        return self

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.watcher = watcher_mod.Watcher(
                self.args.root, os.path.join(HERE, self.args.session),
                store.Config(self.args.root), tg.proxy_dict(self.args.proxy))
        except Exception as exc:
            self.error = exc
        finally:
            self.ready.set()
        if self.error is None:
            self.loop.run_forever()

    def serve(self):
        """开监听：固定绑 0.0.0.0，本机和手机都能连。

        以前这里按「局域网开关」决定绑 127.0.0.1 还是 0.0.0.0，还得能当场改绑；
        现在开关没了，就启动时绑一次。密码门在 Handler._authed()。
        返回实际绑上的地址（起不来返回 None）。
        """
        host = self.args.host or "0.0.0.0"
        try:
            httpd = ThreadingHTTPServer((host, self.args.port), Handler)
        except OSError as exc:
            print(f"监听 {host}:{self.args.port} 起不来：{exc}", flush=True)
            if host == "127.0.0.1":
                return None
            httpd, host = ThreadingHTTPServer(("127.0.0.1", self.args.port), Handler), "127.0.0.1"
            print("退回只绑本机，手机连不上。", flush=True)
        httpd.daemon_threads = True
        Handler.app = self
        self.httpd, self.host = httpd, host
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        if host == "0.0.0.0":
            print(f"手机/局域网地址：http://{_lan_ip()}:{self.args.port}/"
                  + ("（设过访问密码就要输）" if self.watcher.reg.setting("lan_hash", "")
                     else "（还没设密码，同一网络谁都能打开）"), flush=True)
        else:
            print(f"只绑本机：http://127.0.0.1:{self.args.port}/", flush=True)
        return host

    def call(self, coro, timeout=180):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def call_fn(self, fn, *a, **kw):
        async def go():
            return fn(*a, **kw)
        return self.call(go())

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        keep_awake(False)          # 松开电源请求，电脑可以照常待机了
        try:
            self.call(self.watcher.close(), 15)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)


class Handler(BaseHTTPRequestHandler):
    server_version = "tgjt-archive"
    app = None

    def log_message(self, fmt, *a):
        pass                                    # 别把每条请求都刷到控制台

    # --- 输出 ---

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except CONN_GONE:
            pass                     # 客户端提前走了，不算错，别往日志里刷 traceback

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _locked_denied(self):
        """锁着的时候库接口一律 423 —— 锁着的服务就该是真锁着，手机也一样。"""
        if not self.app.watcher.locked:
            return False
        path = self.path.split("?")[0]
        if path in LOCKED_OK or not path.startswith(("/api/", "/media")):
            return False
        self._json({"error": "服务锁着：去电脑上的网页输口令解锁", "locked": True}, 423)
        return True

    def _file(self, path, download=False):
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        rng = parse_range(self.headers.get("Range"), size)
        start, end, code = (rng[0], rng[1], 206) if rng else (0, size - 1, 200)
        length = end - start + 1
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download:
                # 手机上「保存」用：让浏览器存成文件而不是内嵌播放
                self.send_header("Content-Disposition",
                                 "attachment; filename*=UTF-8''" + quote(os.path.basename(path)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            with open(path, "rb") as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except CONN_GONE:
            # 手机拖进度条、切页、退出时就是会中途断；这是正常的，不是错误。
            pass

    # --- 路由 ---

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not self._authed():
            return self._login_page()
        if self._locked_denied():
            return
        try:
            if u.path in ("/", "/index.html"):
                return self._static("index.html")
            name = u.path.lstrip("/")
            if name in STATIC:
                return self._static(name)
            if u.path.startswith("/media/"):
                return self._media(u.path[7:], "dl=1" in (u.query or ""))
            if u.path == "/api/export.csv":
                return self._csv(self.app.call_fn(self._csv_bytes, q))
            if u.path == "/api/status":
                st = self.app.call(self.app.watcher.status())
                st["local"] = is_local(self.client_address[0])
                st["downloading"] = self.app.call(self.app.watcher.downloading())
                # 抽屉要如实显示实际监听地址（绑定固定，地址只在退回本机时才没有）。
                # lan_password_set 由 watcher.status() 在 loop 线程上读库，这里只补内存里的。
                st["lan_host"] = self.app.host
                st["lan_url"] = (f"http://{_lan_ip()}:{self.app.args.port}/"
                                 if self.app.host == "0.0.0.0" else None)
                st["keepawake"] = AWAKE          # 电源请求设上了没有（进待机就没人应答手机）
                return self._json(st)
            if u.path == "/api/chats":
                return self._json({"chats": self.app.call(
                    self.app.watcher.chats(q.get("force") == "1"), 120)})
            if u.path == "/api/items":
                return self._json(self.app.call_fn(self._items, q))
            if u.path == "/api/authors":
                return self._json(self.app.call_fn(self._authors, q))
            if u.path == "/api/topics":
                return self._json(self.app.call_fn(self._topics, q))
            if u.path == "/api/top":
                return self._json(self.app.call_fn(self._top, q))
            if u.path == "/api/stats":
                return self._json(self.app.call_fn(self._stats, q))
            self._json({"error": "no route"}, 404)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/login":              # 表单提交，不是 JSON，先单独处理
            return self._login()
        if not self._authed():
            return self._login_page()
        body = self._body()
        if self._locked_denied():
            return
        try:
            if u.path == "/api/login/qr":
                return self._json(self.app.call(self.app.watcher.login_qr(), 90))
            if u.path == "/api/login/password":
                return self._json(self.app.call(
                    self.app.watcher.login_password(body.get("password") or ""), 90))
            if u.path == "/api/config":
                return self._json(self.app.call(self.app.watcher.set_config(
                    body.get("api_id"), body.get("api_hash")), 120))
            if u.path == "/api/retry":
                return self._json(self.app.call(self.app.watcher.retry(), 120))
            if u.path == "/api/retryone":
                return self._json(self.app.call_fn(self._retryone, body))
            if u.path in LOCK_LOCAL:            # 口令只在本机输入：HTTP 是明文，不能走局域网
                if not is_local(self.client_address[0]):
                    return self._json({"error": "只有本机能用（口令不能走局域网明文）"}, 403)
                w = self.app.watcher
                if u.path == "/api/unlock":
                    return self._json(self.app.call(w.unlock(body.get("passcode") or ""), 120))
                if u.path == "/api/passcode":
                    pw = body.get("passcode") or ""
                    return self._json(self.app.call(
                        w.set_passcode(pw) if pw else w.clear_passcode(), 120))
                if u.path == "/api/session/reset":
                    return self._json(self.app.call(w.reset_session(), 120))
                return self._json({"error": "no route"}, 404)
            if u.path == "/api/backfill":
                return self._json({"added": self.app.call(self.app.watcher.backfill(
                    int(body["chat_id"]), float(body.get("hours") or 1)), 900)})
            if u.path == "/api/watch":
                return self._json(self.app.call_fn(self._set_watch, body))
            if u.path == "/api/settings":
                return self._json(self.app.call_fn(self._set_settings, body))
            if u.path == "/api/delete":
                return self._json(self.app.call_fn(self._delete, body))
            if u.path == "/api/star":
                return self._json(self.app.call_fn(self._star, body))
            if u.path == "/api/zip":
                return self._json(self.app.call_fn(self._zip, q))
            if u.path in ("/api/reveal", "/api/open"):
                if not is_local(self.client_address[0]):
                    return self._json({"error": "只有本机能用"}, 403)
                return self._json(self.app.call_fn(self._reveal, u.path, body))
            self._json({"error": "no route"}, 404)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # --- 具体处理（跑在 loop 线程上）---

    def _authed(self):
        """本机免密码；局域网必须带对 cookie。所有路由都过这道门，包括 /media 直链。"""
        if self._local():
            return True
        tok = self._token()
        if not tok:
            return False
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "arc" and hmac.compare_digest(v.strip(), tok):
                return True
        return False

    def _local(self):
        return is_local(self.client_address[0])

    def _token(self):
        # 库连接属于 loop 线程，HTTP 线程直接读会抛 ProgrammingError（手机端就登不进来了）
        want = self.app.call_fn(lambda: self.app.watcher.reg.setting("lan_hash", ""))
        if not want:
            return None
        return hmac.new(want.encode("ascii"), b"archive-lan", hashlib.sha256).hexdigest()

    def _login_page(self, err=""):
        body = LOGIN_HTML.replace("{err}", f'<div class="e">{err}</div>' if err else "")
        raw = body.encode("utf-8")
        self.send_response(401 if err else 200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _login(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        pw = (form.get("pw") or [""])[0]
        salt, want = self.app.call_fn(
            lambda: (self.app.watcher.reg.setting("lan_salt", ""),
                     self.app.watcher.reg.setting("lan_hash", "")))
        if not check_pw(pw, salt, want):
            return self._login_page("密码不对")
        self.send_response(303)
        self.send_header("Set-Cookie",
                         f"arc={self._token()}; Path=/; Max-Age=2592000; SameSite=Lax")
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _static(self, name):
        path = os.path.join(WEB, name)
        if not os.path.isfile(path):
            return self._json({"error": f"缺少 {name}"}, 500)
        body = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", STATIC[name])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _media(self, rest, download=False):
        chat, _, rel = rest.partition("/")
        try:
            chat_id = int(chat)
        except ValueError:
            return self._json({"error": "bad chat"}, 400)
        root = os.path.join(self.app.args.root, str(chat_id))
        path = safe_lib_path(root, rel)
        if path is None or not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        self._file(path, download)

    def _items(self, q):
        chat_id = int(q["chat"])
        st = self.app.watcher.store(chat_id)
        kinds = [k for k in (q.get("kinds") or "").split(",") if k]
        # 列表、导出、总数都走同一套筛选参数，少一个地方不一致就会「导出跟屏幕对不上」
        flt = {"kinds": kinds or None, "status": q.get("status") or None,
               "author": q.get("author") or None, "topic": q.get("topic") or None}
        rows = st.items(after_msg=int(q["after"]) if q.get("after") else None,
                        before_msg=int(q["before"]) if q.get("before") else None,
                        limit=min(int(q.get("limit") or 60), 500), **flt)
        # 编辑历史：只有换过媒体的行才有。卡片显示原图、展开列全部版本都靠它。
        revs = {r["msg_id"]: st.revisions(r["msg_id"]) for r in rows if r["edited"]}
        groups = gallery.group_items(rows, revs)
        if is_local(self.client_address[0]):
            # 绝对路径只发给本机：磁盘路径对手机没意义，也不该到处传
            base = os.path.join(self.app.args.root, str(chat_id))
            for g in groups:
                for m in g["members"]:
                    m["abs"] = (os.path.join(base, m["file"].replace("/", os.sep))
                                if m["file"] else None)
        # rows / min_msg 给前端翻页用：卡片数是去重后的，比行数少，
        # 拿卡片数判断「还有没有更老的」会让自动加载提前停。
        # total 是同一套筛选下的总数（不只是这一页）——顶部的「共 N 条」和导出提示都用它。
        return {"groups": groups, "rows": len(rows), "total": st.count(**flt),
                "min_msg": min((r["msg_id"] for r in rows), default=None),
                "used": st.used_bytes()}

    def _authors(self, q):
        st = self.app.watcher.store(int(q["chat"]))
        return {"authors": [dict(r) for r in st.authors()]}

    def _topics(self, q):
        st = self.app.watcher.store(int(q["chat"]))
        return {"topics": st.topics()}

    def _top(self, q):
        st = self.app.watcher.store(int(q["chat"]))
        return {"items": [dict(r) for r in st.top(min(int(q.get("limit") or 200), 1000))]}

    def _star(self, body):
        st = self.app.watcher.store(int(body["chat_id"]))
        ids = body.get("msg_ids") or [body.get("msg_id")]
        on = bool(body.get("on"))
        n = 0
        for i in ids:
            if i is not None:
                st.set_star(int(i), on)
                n += 1
        return {"on": on, "n": n}

    @staticmethod
    def _versions(st, r):
        """一条消息的所有版本，按时间排：原图 → 每次编辑前 → 现在。

        没编辑过就只有「现在」一行。换过媒体的行靠这个把旧内容也留在清单里 ——
        「编辑不删内容」在导出的清单里同样成立。
        """
        out = []
        if r["edited"]:
            for i, rev in enumerate(st.revisions(r["msg_id"])):
                out.append(("原图" if i == 0 else f"第 {i + 1} 次编辑前",
                            rev["kind"] or r["kind"], rev["size"] or 0,
                            rev["caption"], rev["path"]))
        out.append(("现在", r["kind"], r["size"] or 0, r["caption"], r["link"] or r["path"]))
        return out

    def _csv_bytes(self, q):
        """在 loop 线程上把清单拼出来（库连接不能跨线程用，这是这个项目的老规矩）。

        导的是**当前筛选下的全部条目**，不是屏幕上这一页；换过媒体的行每个版本各占一行。
        """
        chat_id = int(q["chat"])
        st = self.app.watcher.store(chat_id)
        kinds = [k for k in (q.get("kinds") or "").split(",") if k]
        rows = st.items(limit=200000, kinds=kinds or None, status=q.get("status") or None,
                        author=q.get("author") or None, topic=q.get("topic") or None)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["msg_id", "时间", "作者ID", "作者", "类型", "字节", "状态", "版本", "话题",
                    "说明", "原消息", "磁盘路径", "已编辑", "已删除"])
        for r in rows:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["date"] or 0))
            for label, kind, size, caption, rel in self._versions(st, r):
                w.writerow([r["msg_id"], when, r["author_id"], r["author_name"] or "", kind,
                            size, r["status"], label,
                            r["topic_title"] or r["topic_id"] or "",
                            (caption or "").replace("\n", " "), r["permalink"] or "",
                            st.abs(rel) if rel else "",
                            "是" if r["edited"] else "", "是" if r["deleted"] else ""])
        return {"chat": chat_id, "raw": "\ufeff" + buf.getvalue()}

    def _csv(self, data):
        raw = data["raw"].encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         f'attachment; filename="archive-{data["chat"]}.csv"')
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _zip(self, q):
        """把当前筛选的文件打包到库里的 tmp/ 下，返回路径。

        媒体本来就是压缩格式，用 STORED 不再压一遍，快很多。
        换过媒体的行，历史版本也一起进包（history/<消息号>/<序号>-<文件名>）——
        「编辑不删内容」在导出的包里也成立。
        """
        chat_id = int(q["chat"])
        st = self.app.watcher.store(chat_id)
        kinds = [k for k in (q.get("kinds") or "").split(",") if k]
        rows = st.items(limit=200000, kinds=kinds or None, status=q.get("status") or None,
                        author=q.get("author") or None, topic=q.get("topic") or None)
        # 只算真会进包的那些：跳过/等待中的没有文件，重复行的内容已经在原件那条里
        packed = [r for r in rows if not r["dup_of"] and (r["link"] or r["path"])]
        total = sum((r["size"] or 0) for r in packed)
        if total > 6 << 30:
            return {"error": f"这一批有 {total >> 30} GB，太大了。先按作者或话题筛一下再打包"}
        name = "export-%s-%s.zip" % (chat_id, time.strftime("%Y%m%d-%H%M%S"))
        out = st.abs("tmp/" + name)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        n = hist = 0
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for r in packed:
                rel = r["link"] or r["path"]
                full = st.abs(rel)
                if os.path.isfile(full):
                    z.write(full, arcname=rel)
                    n += 1
                for rev in st.revisions(r["msg_id"]):
                    if not rev["path"]:
                        continue
                    rf = st.abs(rev["path"])
                    if os.path.isfile(rf):
                        z.write(rf, arcname="history/%d/%d-%s"
                                % (r["msg_id"], rev["seq"], os.path.basename(rev["path"])))
                        hist += 1
        return {"ok": True, "file": out, "name": name, "n": n, "hist": hist,
                "size": os.path.getsize(out)}

    def _delete(self, body):
        w = self.app.watcher
        chat_id = int(body["chat_id"])
        st = w.store(chat_id)
        if body.get("current") and body.get("msg_id"):   # 删「现在」：回退一版，不删整条
            out = st.drop_current(int(body["msg_id"])) or {"deleted": 0, "bytes": 0}
            if out["deleted"]:
                w.export_static(chat_id)     # 整条被删掉了才需要重导静态页
            else:
                # 顶上来的那版要在 by-author 目录里补个正确名字（drop_current 把 link
                # 清了；relink 失败只吞 OSError，页面照样能走 path 看）
                w.relink(chat_id, int(body["msg_id"]))
            return out
        if body.get("status"):
            out = st.delete_status(body["status"])
        elif body.get("deleted"):
            out = st.delete_deleted()
        elif body.get("rev_seq") is not None:    # 只删「已编辑」的一版历史
            free = st.drop_revision(int(body["msg_id"]), int(body["rev_seq"]))
            out = {"count": 0 if free is None else 1, "freed": free or 0,
                   "deleted": free is not None}
        else:
            out = st.delete([int(i) for i in (body.get("msg_ids") or [])])
        if out["deleted"]:
            w.export_static(chat_id)     # 导过静态页就顺手重生成，免得删了还留在离线页里
        return out

    def _reveal(self, path, body):
        """在资源管理器中显示 / 用默认播放器打开。只有本机能调，服务端再校验一次。"""
        if body.get("path"):                  # 打包出来的 zip 之类：只允许库目录里的
            full = os.path.abspath(str(body["path"]))
            root = os.path.abspath(self.app.args.root)
            if not full.startswith(root + os.sep):
                return {"error": "只允许库目录里的路径"}
            if not os.path.exists(full):
                return {"error": "文件不在了"}
            show_in_explorer(full)
            return {"ok": True, "path": full}
        st = self.app.watcher.store(int(body["chat_id"]))
        row = st.get(int(body["msg_id"]))
        if row is None:
            return {"error": "没有这条"}
        rel = row["link"] or row["path"]
        if not rel:
            return {"error": "这条没有文件（跳过的只有索引）"}
        full = st.abs(rel)
        if not os.path.isfile(full):
            return {"error": "文件不在了：" + full}
        if path == "/api/open":
            os.startfile(full)
        else:
            show_in_explorer(full)
        return {"ok": True, "path": full}

    def _stats(self, q):
        w = self.app.watcher
        counts, used = {}, w.used_bytes()
        for st in w.stores.values():
            for status, (n, _b) in st.counts().items():
                counts[status] = counts.get(status, 0) + n
        cap = w.reg.disk_cap()
        return {"used": used, "cap": cap, "counts": counts,
                "over_cap": bool(cap and used >= cap),
                "sweeps": {str(k): int(v) for k, v in w.last_sweep.items()},
                "note": w.note}

    def _retryone(self, body):
        w = self.app.watcher
        return w.store(int(body["chat_id"])).retry_one(int(body["msg_id"]))

    def _set_watch(self, body):
        reg = self.app.watcher.reg
        chat_id = int(body["chat_id"])
        kinds = body.get("kinds")
        row = reg.set_watch(chat_id,
                            title=body.get("title"),
                            kinds=",".join(kinds) if kinds is not None else None,
                            max_mb=int(body["max_mb"]) if body.get("max_mb") else None,
                            enabled=body.get("enabled"),
                            authors=",".join(body.get("authors") or [])
                            if body.get("authors") is not None else None)
        self.app.watcher.store(chat_id)          # 顺手把库建出来
        return {"watch": dict(row)}

    def _set_settings(self, body):
        reg = self.app.watcher.reg
        if body.get("lan_password"):
            salt, h = hash_pw(str(body["lan_password"]))
            reg.set_setting("lan_salt", salt)
            reg.set_setting("lan_hash", h)
        if body.get("disk_cap_gb") is not None:
            reg.set_setting("disk_cap_gb", max(1, int(body["disk_cap_gb"])))
        return {"disk_cap_gb": reg.setting("disk_cap_gb", 50),
                "lan_host": self.app.host,
                "lan_url": f"http://{_lan_ip()}:{self.app.args.port}/",
                "lan_password_set": bool(reg.setting("lan_hash", ""))}


def main():
    install_log()                       # 先接管输出，连 argparse 报错都进日志
    p = argparse.ArgumentParser(description="受保护群图片/视频归档 —— 本地网页服务")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--root", default=os.path.join(HERE, "library"))
    p.add_argument("--session", default="archive", help="session 文件名（默认 archive）")
    p.add_argument("--proxy", default=tg.DEFAULT_PROXY, help="代理，留空表示直连")
    p.add_argument("--host", default=None,
                   help="绑定地址。默认 0.0.0.0（本机 + 手机都能连）")
    p.add_argument("--no-browser", action="store_true", help="不自动开浏览器")
    args = p.parse_args()

    reg = store.Registry(args.root).open()
    pid_path = os.path.join(HERE, PID_NAME)
    try:
        with open(pid_path, "w", encoding="ascii") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pid_path = None

    app = App(args).start()
    Handler.app = app
    print(f"归档服务已启动，库目录 {args.root}", flush=True)
    print(f"进程号 {os.getpid()}", flush=True)
    print(f"日志（无窗口模式下唯一能看到输出的地方）：{os.path.join(HERE, LOG_NAME)}", flush=True)
    print("api_id 在网页上填一次就存在本机，以后不用再填（api_hash 只在重新登录时才要）。", flush=True)
    print("登录凭证是加密存的（Windows DPAPI），明文只在内存里。", flush=True)
    if reg.setting("pw_salt", ""):
        print("设了口令：服务现在是锁着的，去电脑上打开网页输口令才会连 Telegram。", flush=True)
    print("关掉这个进程就停止监听。游标存在库里，下次启动会自动补上漏掉的那段。", flush=True)
    # 得在主线程上设：这个状态是「线程的」，而主线程会一直活到进程结束。
    global AWAKE
    AWAKE = keep_awake()
    print("服务运行期间不让电脑进待机（屏幕该黑还是黑），手机随时能连。" if AWAKE else
          "没能阻止电脑待机：系统一进待机，Windows 会把服务挂起，手机就连不上了。", flush=True)
    # 临时侦查仪器：心跳线程记「服务活着 + 当前 IP」的时间轴，复现「手机连不上」后按图索骥
    threading.Thread(target=hb_loop, args=(args.port,), daemon=True, name="hb").start()
    if app.call_fn(app.serve) is None:        # 监听在自己的线程里跑；库只能由 loop 线程碰
        print(f"端口 {args.port} 起不来。多半是已经开了一个："
              f"换个端口试试 server.py --port {args.port + 1}", flush=True)
        app.stop()
        if pid_path:
            try:
                os.remove(pid_path)
            except OSError:
                pass
        return 1
    print(f"本机打开：http://127.0.0.1:{args.port}/", flush=True)
    if not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{args.port}/")
    try:
        threading.Event().wait()               # 只是等 Ctrl+C / 进程被杀
    except KeyboardInterrupt:
        print("收工", flush=True)
    finally:
        app.stop()
        if pid_path:
            try:
                os.remove(pid_path)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
