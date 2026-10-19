"""实机验证「启动口令」这条路：起一个**真的** server.py（另一个端口、另一个库目录），
对着它发真的 HTTP 请求，看锁着的时候到底拦不拦、口令到底认不认。

不碰你正在用的 archive.session，也不碰主服务的库和日志（子进程的日志写进 _tests/_locktest.log）。
唯一会动到的是 _server.pid —— 脚本结束时还原成主服务那个。

    python -X utf8 _tests/_lock_live.py
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))   # 本脚本所在目录（_tests）
ARCH = os.path.dirname(HERE)                        # 生产代码所在目录
PORT = 8788
ROOT = os.path.join(HERE, "_locktest")              # 子服务的库：跟着测试目录走，不污染 archive 根
BASE = f"http://127.0.0.1:{PORT}"
PIDFILE = os.path.join(ARCH, "_server.pid")
# 子进程日志也留在测试目录里。server.py 是按 ARCH 拼 LOG_NAME 的，所以这里给相对子路径。
LOGFILE = os.path.relpath(HERE, ARCH).replace(os.sep, "/") + "/_locktest.log"
CHILD = ("import sys, server; server.LOG_NAME = " + repr(LOGFILE)
         + "; sys.exit(server.main())")

fails, oks = [], []


def check(cond, label, detail=""):
    (oks if cond else fails).append(label)
    print(("  OK   " if cond else "  FAIL ") + label + (f"  —— {detail}" if detail else ""),
          flush=True)
    return cond


def req(path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


def start():
    """起一个子服务并等它开始应答。返回 (进程, 第一次的 /api/status)。"""
    p = subprocess.Popen([sys.executable, "-X", "utf8", "-c", CHILD,
                          "--port", str(PORT), "--root", ROOT,
                          "--session", os.path.join(ROOT, "x"), "--no-browser"],
                         cwd=ARCH, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        time.sleep(0.5)
        try:
            code, body = req("/api/status", timeout=5)
            if code == 200:
                return p, body
        except Exception:
            pass
    p.kill()
    raise SystemExit("子服务起不来，看 " + LOGFILE)


def stop(p):
    if p is None:
        return
    p.terminate()
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill()


def main():
    main_pid = ""
    if os.path.isfile(PIDFILE):
        with open(PIDFILE, encoding="ascii") as fh:
            main_pid = fh.read().strip()
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    p = None
    try:
        p, st = start()
        check(st.get("state") == "setup", "干净的库：state=setup（没 session、没 api_id）",
              st.get("state"))
        check(st.get("locked") is False and st.get("passcode_set") is False,
              "没设口令时不锁（默认档行为不变）")
        check(st.get("legacy_file") is False, "干净目录里没有明文的 session 文件")

        code, r = req("/api/passcode", {"passcode": "123"})
        check(r.get("ok") is False, "口令太短不给设", str(r.get("error")))

        code, r = req("/api/passcode", {"passcode": "hunter2"})
        check(r.get("ok") is True, "设口令成功", str(r))
        code, st = req("/api/status")
        check(st.get("passcode_set") is True and st.get("locked") is False,
              "设完当场还是解锁态（不用马上再输一次）")

        stop(p)
        p, st = start()
        check(st.get("state") == "locked" and st.get("locked") is True,
              "重启后必须锁着", st.get("state"))
        check(st.get("connected") is False, "锁着的时候没有连 Telegram")

        code, r = req("/api/items?chat=-100")
        check(code == 423, "锁着时库接口返回 423（手机也一样）", f"HTTP {code}")
        code, st = req("/api/status")
        check(code == 200, "锁着时 /api/status 还问得到（界面要靠它画锁屏）")

        code, r = req("/api/unlock", {"passcode": "hunter3"})
        check(r.get("ok") is False, "错口令解不开", str(r.get("error")))
        code, r = req("/api/unlock", {"passcode": "hunter2"})
        check(r.get("ok") is True, "对口令解得开", str(r.get("error", "")))
        code, st = req("/api/status")
        check(st.get("locked") is False and st.get("state") != "locked",
              "解锁后不再是锁态", st.get("state"))
        code, r = req("/api/items?chat=-100")
        check(code != 423, "解锁后库接口放行", f"HTTP {code}")

        code, r = req("/api/passcode", {"passcode": ""})
        check(r.get("ok") is True, "取消口令")

        stop(p)
        p, st = start()
        check(st.get("locked") is False and st.get("passcode_set") is False,
              "取消口令后重启不再锁")

        con = sqlite3.connect("file:" + os.path.join(ROOT, "index.db").replace(os.sep, "/")
                              + "?mode=ro", uri=True)
        kv = dict(con.execute("SELECT key,value FROM settings"))
        con.close()
        check("hunter2" not in json.dumps(kv), "库里搜不到明文口令")
        check(not kv.get("pw_salt") and not kv.get("pw_check"),
              "取消口令后盐和校验串都清掉了")
    finally:
        stop(p)
        if main_pid:                       # 把 _server.pid 还给主服务
            with open(PIDFILE, "w", encoding="ascii") as fh:
                fh.write(main_pid)

    print()
    if fails:
        print(f"{len(fails)} 项没过（子服务的日志留在 {LOGFILE}）：")
        for f in fails:
            print("  -", f)
        return 1
    shutil.rmtree(ROOT, ignore_errors=True)
    for junk in ("_locktest.log",):
        try:
            os.remove(os.path.join(HERE, junk))
        except OSError:
            pass
    print(f"全部通过（{len(oks)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
