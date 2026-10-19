"""对着**真实运行的服务 + 真实的库**核对，不造任何假数据。

    python -X utf8 _tests/_check_live.py

离线自检（_tests/_selftest.py）验的是纯逻辑；这个脚本验的是「现在这台机器上真的对不对」：
盘上文件是不是真的完整、队列有没有停摆、服务实际监听在哪、浏览器拿到的是不是新代码。

只读：不改库、不改文件、不发消息（唯一的写入是往服务发一个只读的 GET）。
"""

import base64
import contextlib
import csv
import io
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))   # 本脚本所在目录（_tests）
ARCH = os.path.dirname(HERE)                        # 生产代码所在目录：库、日志都在那儿
ROOT = os.path.join(ARCH, "library")
PORT = int(os.environ.get("TGJT_PORT", "8787"))
BASE = f"http://127.0.0.1:{PORT}"

fails = []
oks = []


def check(cond, label, detail=""):
    (oks if cond else fails).append(label)
    print(("  OK   " if cond else "  FAIL ") + label + (f"  —— {detail}" if detail else ""),
          flush=True)
    return cond


def get(path, raw=False):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        body = r.read()
    return body if raw else json.loads(body.decode("utf-8"))


def open_db(db):
    """只读打开。别写成生成器 —— 那样 with 还没进就把连接关了。"""
    con = sqlite3.connect("file:" + db.replace(os.sep, "/") + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def main():
    print(f"服务：{BASE}\n库目录：{ROOT}\n")

    # --- 1. 服务活着，关键字段齐 ---
    try:
        st = get("/api/status")
    except Exception as exc:
        print(f"  FAIL 连不上服务：{type(exc).__name__}: {exc}")
        print("\n服务没在跑就先 run.bat。")
        return 1
    check(st.get("state") == "ok", "服务已登录（state=ok）", st.get("state"))
    for k in ("local", "downloading", "lan_host", "lan_password_set"):
        check(k in st, f"/api/status 有 {k} 字段")
    check(isinstance(st.get("downloading"), dict), "downloading 是个对象（没下好时为空）")

    # --- 2. 监听地址：绑定固定是 0.0.0.0，手机才能连（以前跟「局域网开关」联动）---
    host = st.get("lan_host")
    check(host is not None, "服务报告了实际监听地址")
    check(host == "0.0.0.0",
          f"监听地址是 0.0.0.0（手机/本机都能连）", host)
    check(bool(st.get("lan_url")), "给了可用的局域网地址（抽屉里要显示给手机）", st.get("lan_url"))
    check("lan" not in st, "已经不再有「局域网开关」这个字段")

    # --- 3. 每个群：盘上文件真的完整 ---
    chats = []
    for name in sorted(os.listdir(ROOT)) if os.path.isdir(ROOT) else []:
        p = os.path.join(ROOT, name)
        if name.startswith("-") and os.path.isdir(p) and os.path.exists(os.path.join(p, "index.db")):
            chats.append((name, p))
    check(bool(chats), "库目录里至少有一个群", str([c[0] for c in chats]))

    for name, p in chats:
        db = os.path.join(p, "index.db")
        with contextlib.closing(open_db(db)) as con:
            # size 完整性只对 status='done' 的行承诺：落盘闸门（.part 大小==total）只在那条
            # 路上把过关。已被编辑换过媒体、新文件又没下成的行（failed/pending），size 列
            # 描述的是新媒体，盘上躺着的是按设计留着的旧版文件 —— 两边不一致是设计内状态。
            bad, missing = [], 0
            for r in con.execute("SELECT msg_id,size,path FROM messages "
                                 "WHERE path IS NOT NULL AND status='done'"):
                f = os.path.join(p, r["path"].replace("/", os.sep))
                if not os.path.exists(f):
                    missing += 1
                elif os.path.getsize(f) != r["size"]:
                    bad.append((r["msg_id"], r["size"], os.path.getsize(f)))
            check(not bad and not missing,
                  f"[{name}] 有文件的行，盘上大小都和 Telegram 报的一致",
                  f"不一致 {bad[:3]}，缺文件 {missing}")

            # 停摆检测：有 pending 但半成品一动不动、还有一堆错 → 队列卡住了
            pend = con.execute("SELECT COUNT(*) FROM messages WHERE status='pending'").fetchone()[0]
            parts = {}
            tdir = os.path.join(p, "tmp")
            for f in (os.listdir(tdir) if os.path.isdir(tdir) else []):
                if f.endswith(".part"):
                    parts[f] = os.path.getsize(os.path.join(tdir, f))
            check(not (pend and st.get("downloading") == {} and pend > 8),
                  f"[{name}] 没有「一堆 pending 却什么都没在下」的停摆",
                  f"pending {pend} 条，正在下 {len(parts)} 个半成品")

            # dup 指针无环（有环会让两张卡一起从画廊里消失）
            ptr = {r["msg_id"]: r["dup_of"] for r in
                   con.execute("SELECT msg_id,dup_of FROM messages WHERE dup_of IS NOT NULL")}
            cyc = []
            for m in ptr:
                seen, cur = set(), m
                while cur in ptr:
                    if cur in seen:
                        cyc.append(m)
                        break
                    seen.add(cur)
                    cur = ptr[cur]
            check(not cyc, f"[{name}] 重复指针没有环", str(cyc[:5]))

            # 相册成员的媒体都要能播：有文件的视频，kind 必须是 video
            odd = [(r["msg_id"], r["kind"]) for r in con.execute(
                "SELECT msg_id,kind FROM messages WHERE path IS NOT NULL AND kind NOT IN "
                "('photo','video')")]
            check(not odd, f"[{name}] 下好的行 kind 都是 photo/video", str(odd[:5]))

            # 编辑换媒体的行永远不算「重复」：那是各自不同的原图，合并成一张卡就错了
            bad = [(r["msg_id"], r["dup_of"]) for r in con.execute(
                "SELECT msg_id,dup_of FROM messages WHERE edited IS NOT NULL "
                "AND dup_of IS NOT NULL")]
            check(not bad, f"[{name}] 被编辑过的行没有一条被标成重复", str(bad[:5]))

            # 编辑留下的历史版本「编辑不删内容」：有 path 的文件必须在盘上；
            # 文件没下好就编辑的行按设计只有缩略图 —— 那么缩略图必须在盘上。
            gone = []
            for r in con.execute("SELECT msg_id,seq,path,thumb FROM revisions"):
                keep = r["path"] or r["thumb"]
                f = os.path.join(p, keep.replace("/", os.sep)) if keep else None
                if not f or not os.path.isfile(f):
                    gone.append((r["msg_id"], r["seq"], keep))
            check(not gone, f"[{name}] 历史版本的文件都在盘上", str(gone[:5]))

            # 静态离线页和网页端一个口径：有历史版本的卡片，离线页里也有「已编辑」版本条
            page = os.path.join(p, "index.html")
            if os.path.isfile(page):
                with open(page, encoding="utf-8", errors="replace") as fh:
                    html = fh.read()
                nrev = con.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
                nstrip = html.count('<details class="revs">')
                check(nrev == 0 or nstrip >= 1,
                      f"[{name}] 静态离线页也有「已编辑」版本条（和网页端一致）",
                      f"库里 {nrev} 个历史版本，离线页 {nstrip} 条版本条")

    # --- 4. 画廊接口和库对得上 ---
    # 用内容最多的那个群来查：新加的群可能还是空的，空群当然 0 张卡（那不是 bug）
    def _rows(name):
        with contextlib.closing(open_db(os.path.join(ROOT, name, "index.db"))) as con:
            return con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    chats.sort(key=lambda c: _rows(c[0]), reverse=True)
    if chats:
        cid = int(chats[0][0])
        d = get(f"/api/items?chat={cid}&limit=500")
        gs = d.get("groups") or []
        check(bool(gs), "画廊有卡片", f"{len(gs)} 张")
        check(all("dups" in g for g in gs), "每张卡都带 dups 字段（前端要用）")
        n_msg = sum(len(g["members"]) for g in gs)
        n_dup = sum(len(g.get("dups") or []) for g in gs)
        # 只跟本页窗口的去重前行数（接口自己报的 rows）对账：库超过一页后按整库对数
        # 会把没翻到的页误判成「丢行」（假红，注释原来就标过），整库覆盖由 4b 逐页翻底兜。
        check(n_msg + n_dup == (d.get("rows") or 0),
              "卡片里的消息行 + 重复挂载 = 本页窗口的去重前行数（一行都没丢）",
              f"{n_msg}+{n_dup} vs rows={d.get('rows')}")

        # 上面这条是按一页 500 行查的，库长过 500 行就会假红；4b 的翻页检查才是真兜底。
        check("rows" in d and "min_msg" in d, "接口给了翻页要用的 rows / min_msg",
              str(sorted(d.keys())))

        # --- 4d. 列表顺序就是消息顺序：最上面一张卡一定是最新的那条 ---
        # 卡片按「组里最大的消息号」倒序；相册内部按消息号升序（跟客户端一致），
        # 所以只能按每张卡的最大号比，不能把成员摊平了比。
        tops = [max(m["msg_id"] for m in g["members"]) for g in d.get("groups") or []]
        check(tops == sorted(tops, reverse=True),
              "列表按消息号倒序（最上面是最新的，补进来的老消息不会插到顶上）",
              f"前几个 {tops[:5]}")
        with contextlib.closing(
                open_db(os.path.join(ROOT, chats[0][0], "index.db"))) as con:
            top_msg = con.execute("SELECT MAX(msg_id) FROM messages").fetchone()[0]
        check(tops and tops[0] == top_msg, "列表第一张卡就是库里消息号最大的那条",
              f"{tops[:1]} vs 库里最大 {top_msg}")

        # --- 4e. 电源请求：服务握着 System Required，系统才不会进待机把服务挂起 ---
        stt = get("/api/status")
        check(stt.get("keepawake") is True,
              "服务握着电源请求（系统进待机时它不会被 DAM 挂起，手机随时能连）",
              str(stt.get("keepawake")))

        # --- 4c. 顶部条数和导出都是「当前筛选下的全部」，不是当前页 ---
        with contextlib.closing(
                open_db(os.path.join(ROOT, chats[0][0], "index.db"))) as con:
            db_rows = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            db_revs = con.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
            au = con.execute("SELECT author_id, COUNT(*) n FROM messages GROUP BY author_id "
                             "ORDER BY n DESC LIMIT 1").fetchone()
            au_revs = con.execute(
                "SELECT COUNT(*) FROM revisions r JOIN messages m "
                "ON m.chat_id=r.chat_id AND m.msg_id=r.msg_id WHERE m.author_id=?",
                (au["author_id"],)).fetchone()[0]
        check(d.get("total") == db_rows, "接口给的 total 是全部条数（不是这一页）",
              f"{d.get('total')} vs 库里 {db_rows}")

        def csv_n(qs=""):
            raw = get(f"/api/export.csv?chat={cid}{qs}", raw=True).decode("utf-8-sig")
            return list(csv.reader(io.StringIO(raw)))

        lines = csv_n()
        check(bool(lines) and lines[0][7] == "版本", "导出清单有「版本」列（历史版本各占一行）",
              str(lines[0] if lines else []))
        check(len(lines) - 1 == db_rows + db_revs,
              "导出清单导的是全部条目（不是当前页），每个历史版本各占一行",
              f"清单 {len(lines) - 1} 行 vs 库里 {db_rows} 条 + {db_revs} 个历史版本")
        auq = f"&author={au['author_id']}"
        check(len(csv_n(auq)) - 1 == au["n"] + au_revs,
              "导出清单认筛选（按作者导的就是这个作者的那些）",
              f"{au['author_id']}：{len(csv_n(auq)) - 1} 行 vs 库里 {au['n']} + {au_revs}")

        # --- 4b. 照前端的方式逐页翻到底（limit=60，游标用服务端给的 min_msg）---
        # 验「加载更多」不会提前消失：前端靠每页的 rows（去重前的行数）判断还有没有更老的。
        # 库边翻边在下新东西，所以只对「开始翻之前就存在的行」提要求。
        with contextlib.closing(
                open_db(os.path.join(ROOT, chats[0][0], "index.db"))) as con:
            was = {r[0] for r in con.execute("SELECT msg_id FROM messages")}
        seen, before, pages, short = set(), None, 0, []
        while pages < 80:
            p = get(f"/api/items?chat={cid}&limit=60" + (f"&before={before}" if before else ""))
            pg = p.get("groups") or []
            pages += 1
            if not pg:
                break
            if p.get("rows") is None:
                short.append(pages)
            for g in pg:
                for m in g["members"]:
                    seen.add(m["msg_id"])
                for m in (g.get("dups") or []):
                    seen.add(m["msg_id"])
            nxt = p.get("min_msg")
            if not nxt or nxt == before:      # 没有更老的了，或者游标没往前走（防死循环）
                break
            before = nxt
        miss = was - seen
        check(not miss, "逐页翻到底能覆盖库里每一条消息（翻页不丢行）",
              f"翻了 {pages} 页，缺 {len(miss)} 条 {sorted(miss)[:5]}")
        check(not short, "每页都报了去重前的行数 rows（前端靠它决定要不要显示「加载更多」）",
              f"这些页没报：{short}")

    # --- 5. 凭证加密：盘上不该有明文 ---
    with contextlib.closing(open_db(os.path.join(ROOT, "index.db"))) as con:
        kv = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM settings")}
    check("api_hash" not in kv, "settings 里没有明文 api_hash 那一格（老版本才那样存）")
    enc = kv.get("api_hash_enc") or ""
    check(not enc or len(enc) > 100,
          "api_hash 落盘的是密文（DPAPI 密文几百字节；32 位十六进制那种才是明文）",
          f"{len(enc)} 字节" + ("" if enc else "（这台机器没填过 api_hash，不算错）"))
    blob = kv.get("session_blob") or ""
    if blob:
        check(not base64.b64decode(blob).startswith(b"SQLite format 3"),
              "session 密文不是明文 SQLite（auth_key 没在盘上躺着）")
    else:
        print("  信息 现在还是明文的 archive.session（legacy_file=true）："
              "界面上会提醒换掉，换成加密存储后这条会自动变成密文检查")
    check(isinstance(st.get("locked"), bool) and isinstance(st.get("legacy_file"), bool),
          "/api/status 报了 locked / legacy_file（界面据此画锁屏和提醒）")
    check(not st.get("locked") or st.get("passcode_set"),
          "没设口令就不该出现「锁着」这个状态（两档互斥）",
          f"locked={st.get('locked')} passcode_set={st.get('passcode_set')}")

    # --- 6. 浏览器拿到的是不是新代码（防「改了本地文件，页面跑的还是旧的」）---
    for name, must, mustnot in (
            ("app.js", ["本地播放", "fitMasonry", "dups", "maybeMore", "mountSent",
                        "lockpw", "glock", "drawLock", "pollTimers"],
             ["大屏", "keep_version", "versionStrip", "morebtn", "watchTail", "legacyfix"]),
            ("style.css", ["grid-auto-rows", "pendingmark", ".noside", "#sent"],
             [".vers", ".headrow"]),
            ("index.html", ["lanpw", "sclose", "askyes", "fbtn", "sidebtn", "fpanel",
                            "glock", "dlockpw", "lockoff", "legacywarn"],
             ["lanchk", "morebtn", 'data-tab="all"', 'data-mob="all"',
              "放登录凭证", "legacyfix", "打不开就按顺序查"])):
        try:
            body = get("/" + name, raw=True).decode("utf-8", "replace")
        except Exception as exc:
            check(False, f"/{name} 拿得到", str(exc))
            continue
        miss = [m for m in must if m not in body]
        extra = [m for m in mustnot if m in body]
        check(not miss and not extra, f"/{name} 服务出去的是新版本",
              f"缺 {miss} 多 {extra}")

    # --- 7. 日志里有没有「下载线程出错」（程序 bug 的信号）---
    # 只数**本次启动之后**的：日志是跨次追加的，老的那 598 次永远清不掉，数总数就永远红。
    log = os.path.join(ARCH, "_run.log")
    if os.path.exists(log):
        with open(log, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
        mark = txt.rfind("归档服务已启动")
        run = txt[mark:] if mark >= 0 else txt
        n = len(re.findall(r"下载线程出错", run))
        check(n == 0, "本次启动后日志里没有「下载线程出错」（那是程序 bug，不是网络）",
              f"{n} 次" + (f"（本次启动前有 {len(re.findall(chr(0x4E0B)+'载线程出错', txt)) - n} 次，"
                           "是修之前留下的）" if n == 0 else ""))
        msgs = re.findall(r"\[(\d+)\] ([^\n]+)", run)
        print(f"  信息 本次启动后带消息号的下载提示 {len(msgs)} 条"
              + (f"，最近一条：{msgs[-1][0]} {msgs[-1][1][:40]}" if msgs else ""))

    print()
    if fails:
        print(f"{len(fails)} 项没过：")
        for f in fails:
            print("  -", f)
        return 1
    print(f"全部通过（{len(oks)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
