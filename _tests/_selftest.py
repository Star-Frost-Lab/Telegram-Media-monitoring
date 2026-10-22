"""离线自检：不联网、不需要账号、不起服务。

    python -X utf8 _tests/_selftest.py

重点盯三件事：
  1. 兜底扫描往回翻会不会漏消息（漏消息是这套东西最不可接受的失败）
  2. 去重会不会把同一条内容下两遍
  3. 网页那条路的安全与正确：Range 解析、路径穿越、相册聚合
"""

import asyncio
import csv
import hashlib
import io
import json
import os
import shutil
import sys
import types
import zipfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))   # 本脚本所在目录（_tests）
ARCH = os.path.dirname(HERE)                        # 生产代码所在目录
sys.path.insert(0, ARCH)
import gallery
import secure
import server
import store
import tg
import watcher
from telethon.crypto import AuthKey

MiB = 1 << 20
CHAT = -1001234567890
NOW = datetime.now(timezone.utc)


def bare_watcher(root, cfg=None):
    """不连网的 Watcher：只为测状态机/加密那几段，跳过 __init__ 里的库扫描。"""
    w = watcher.Watcher.__new__(watcher.Watcher)
    w.root = root
    w.cfg = cfg or store.Config(root)
    w.proxy = None
    w.session_file = os.path.join(root, "x.session")
    w.reg = store.Registry(root).open()
    w.sealer = secure.Sealer()
    w.cfg.sealer = w.sealer
    w.blob_session = None
    w.locked = False
    w.pw_fails = 0
    w.pw_until = 0.0
    w.client = None
    w.state, w.error, w.note = "starting", None, ""
    w.hooked = False
    w.worker = w.sweeper = w.qr = w.qr_task = None
    w.running = True
    w.last_sweep = {}
    w._chats_cache, w._chats_at = [], 0
    w.stores = {}
    return w


# --- 造数据 ---

def photo(uid, w=800, h=600, size=123456):
    from telethon.tl.types import Photo, PhotoSize, PhotoSizeProgressive
    return Photo(id=uid, access_hash=1, file_reference=b"0" * 8, date=None, dc_id=5,
                 sizes=[PhotoSize(type="m", w=320, h=240, size=9999),
                        PhotoSizeProgressive(type="y", w=w, h=h, sizes=[1000, size])])


def doc(uid, mime, size, sticker=False, video=None):
    from telethon.tl.types import (Document, DocumentAttributeSticker,
                                   DocumentAttributeVideo, InputStickerSetEmpty)
    attrs = []
    if sticker:
        attrs.append(DocumentAttributeSticker(alt="a", stickerset=InputStickerSetEmpty()))
    if video:
        attrs.append(DocumentAttributeVideo(duration=video[2], w=video[0], h=video[1],
                                            round_message=False, supports_streaming=True))
    return Document(id=uid, access_hash=1, file_reference=b"0" * 8, date=None,
                    mime_type=mime, size=size, dc_id=5, thumbs=[], video_thumbs=[],
                    attributes=attrs)


def msg(mid, **kw):
    base = dict(id=mid, date=NOW, sender_id=555, sender=None, photo=None, document=None,
                reply_to=None, grouped_id=None, message="", post_author=None)
    base.update(kw)
    # Telethon 的 Message.media 就是 photo/document 包出来的，测试里给个等价的真值就行
    base.setdefault("media", kw.get("photo") or kw.get("document"))
    base.setdefault("chat_id", CHAT)          # refetch() 要用它换 input entity
    return types.SimpleNamespace(**base)


def row(chat_id, msg_id, **kw):
    """直接塞库用的完整行（不经过 _take）。"""
    r = {"chat_id": chat_id, "msg_id": msg_id, "topic_id": kw.get("topic_id"),
         "author_id": kw.get("author_id", 555), "date": kw.get("date", int(NOW.timestamp())),
         "kind": kw.get("kind", "photo"), "size": kw.get("size", 1000),
         "mime": "video/mp4" if kw.get("kind") == "video" else "image/jpeg",
         "width": 100, "height": 200, "duration": None, "caption": kw.get("caption"),
         "permalink": tg.permalink_id(chat_id, msg_id, kw.get("topic_id")),
         "grouped_id": kw.get("grouped_id"), "file_uid": kw.get("file_uid", msg_id),
         "sha256": kw.get("sha256"), "path": kw.get("path"), "link": kw.get("link"),
         "thumb": kw.get("thumb"), "status": kw.get("status", "pending"),
         "note": kw.get("note"), "dup_of": kw.get("dup_of"), "tries": 0,
         "edited": kw.get("edited"), "added": kw.get("added", 0),
         "next_try": kw.get("next_try")}
    return r


def fake_watcher(root):
    """绕过 TelegramClient 构造（不连网、不建 session），只测 _take 的逻辑。"""
    w = watcher.Watcher.__new__(watcher.Watcher)
    w.root = root
    w.reg = store.Registry(root).open()
    w.stores = {}
    w.client = types.SimpleNamespace(download_media=None)
    return w


async def main():
    d = os.path.join(HERE, "_selftest_tmp")
    shutil.rmtree(d, ignore_errors=True)   # 放工作目录里，别丢到系统临时文件夹
    os.makedirs(d)

    # --- 1. 收什么不收什么 ---
    assert watcher.classify(msg(1, photo=photo(1)))[0]["kind"] == "photo"
    assert watcher.classify(msg(2, document=doc(2, "video/mp4", 5 * MiB, video=(720, 1280, 17))))[0]["kind"] == "video"
    assert watcher.classify(msg(3, document=doc(3, "image/gif", 900 * 1024)))[0]["kind"] == "photo"
    for bad, why in [(msg(4, document=doc(4, "image/webp", 17342, sticker=True)), "sticker"),
                     (msg(5, document=doc(5, "application/x-tgsticker", 7269, sticker=True)), "sticker"),
                     (msg(6, document=doc(6, "video/webm", 28213, sticker=True, video=(512, 505, 0.74))), "sticker"),
                     (msg(7, document=doc(7, "audio/ogg", 97342)), "audio/ogg"),
                     (msg(8, document=doc(8, "application/pdf", 1000)), "application/pdf"),
                     (msg(9), "no-media")]:
        assert watcher.classify(bad) == (None, why), (bad.id, watcher.classify(bad))
    info = watcher.classify(msg(10, photo=photo(10, 1280, 960, 396993)))[0]
    assert (info["width"], info["height"], info["size"]) == (1280, 960, 396993), info
    print("classify ok: 贴纸/动态 emoji/语音/文件都挡掉，照片和视频认出来，取最大尺寸")

    # --- 2. 兜底扫描往回翻：不能漏、不能重复、翻到头要停 ---
    ids = list(range(101, 111))
    all_msgs = [types.SimpleNamespace(id=i, date=NOW + timedelta(hours=i - 101)) for i in ids]

    def fetch_from(msgs):
        async def fetch(before, limit):
            pool = sorted([m for m in msgs if before == 0 or m.id < before], key=lambda m: -m.id)
            return pool[:limit]
        return fetch

    got = await watcher.fetch_pages(fetch_from(all_msgs), cursor=105, page=3)
    assert [m.id for m in got] == [106, 107, 108, 109, 110], [m.id for m in got]
    got = await watcher.fetch_pages(fetch_from(all_msgs), cursor=0, page=3)
    assert [m.id for m in got] == ids, [m.id for m in got]
    got = await watcher.fetch_pages(fetch_from(all_msgs), cursor=0, page=3, max_pages=2)
    assert [m.id for m in got] == [105, 106, 107, 108, 109, 110], [m.id for m in got]
    got = await watcher.fetch_pages(fetch_from(all_msgs), cursor=200, page=3)
    assert got == [], [m.id for m in got]
    print(f"cursor   ok: 撞游标就停（{len(ids)} 条全在、无缺口），max_pages 能刹住")

    # --- 3. 入库规则（Watcher._take）---
    w = fake_watcher(d)
    w.reg.set_watch(CHAT, title="测试群", kinds="photo,video", max_mb=100, enabled=True)
    wr = w.reg.watch(CHAT)
    topic = types.SimpleNamespace(reply_to_top_id=None, reply_to_msg_id=654278)
    msgs = [msg(1, photo=photo(777)),
            msg(2, photo=photo(777)),                                     # 同一张又发一遍
            msg(3, document=doc(3, "image/webp", 17342, sticker=True)),    # 贴纸
            msg(4, document=doc(4, "video/mp4", 200 * MiB, video=(1920, 1080, 491))),
            msg(5, document=doc(5, "video/mp4", 5 * MiB, video=(720, 1280, 17)),
                reply_to=topic, message="女友", grouped_id=42)]
    got = [await w._take(CHAT, m, wr, thumb=False) for m in msgs]
    assert got == ["pending", "dup", None, "skipped", "pending"], got

    st = w.store(CHAT)
    rows = {r["msg_id"]: r for r in st.gallery_rows()}
    assert set(rows) == {1, 2, 4, 5}, sorted(rows)
    assert rows[2]["dup_of"] == 1 and rows[2]["status"] == "dup"
    assert rows[4]["status"] == "skipped" and "100 MB" in rows[4]["note"]
    assert rows[5]["topic_id"] == 654278 and rows[5]["grouped_id"] == 42
    assert rows[5]["permalink"] == "https://t.me/c/1234567890/654278/5", rows[5]["permalink"]
    assert st.cursor() == 0, "监听器绝不能推进游标，否则兜底扫描就查不出缺口了"

    # 同一条再来一次（兜底扫描会重复看到）：INSERT OR IGNORE，不产生新行
    assert await w._take(CHAT, msgs[0], wr, thumb=False) == "pending"
    assert len(st.gallery_rows()) == 4

    # 撤回裸下的「下载门票」：入库时存 access_hash + file_reference（+全图 thumb_size）
    ref = json.loads(st.get(1)["media_ref"])
    assert ref == {"ty": "p", "ah": 1, "fr": "MDAwMDAwMDA=", "ts": "y"}, ref
    vref = json.loads(st.get(5)["media_ref"])
    assert vref["ty"] == "d" and vref["ts"] == "" and vref["ah"] == 1, vref
    # 行没票的旧行：重看活消息时顺手补上（编辑换掉的媒体不算旧媒体的票，
    # 条件是 file_uid 还对得上 —— 这里 1 号没被编辑，必补）
    st.set_media_ref(1, "")
    st.commit()
    assert await w._take(CHAT, msgs[0], wr, thumb=False) == "pending"
    assert json.loads(st.get(1)["media_ref"]) == ref
    # 坏票不炸：解析失败返回错误说明，由调用方照常走失败分支
    assert (await watcher.grab_by_ref(None, st,
                                      {"media_ref": "{oops", "file_uid": 1}))[1] != ""

    # 类型过滤：只勾视频时，图片不进库
    w.reg.set_watch(CHAT, kinds="video")
    wr = w.reg.watch(CHAT)
    assert await w._take(CHAT, msg(6, photo=photo(888)), wr, thumb=False) is None
    assert 6 not in {r["msg_id"] for r in st.gallery_rows()}

    # 作者白名单：名单里只有 555，别人的（以及没有 sender_id 的系统消息）不进库
    w.reg.set_watch(CHAT, kinds="photo,video", authors="555")
    wr = w.reg.watch(CHAT)
    assert await w._take(CHAT, msg(7, photo=photo(999), sender_id=666), wr, thumb=False) is None
    assert 7 not in {r["msg_id"] for r in st.gallery_rows()}
    assert await w._take(CHAT, msg(8, photo=photo(999), sender_id=None), wr, thumb=False) is None
    assert await w._take(CHAT, msg(21, photo=photo(999), sender_id=666), wr, thumb=False) is None
    assert 21 not in {r["msg_id"] for r in st.gallery_rows()}
    assert await w._take(CHAT, msg(22, photo=photo(999), sender_id=None), wr, thumb=False) is None
    assert await w._take(CHAT, msg(23, photo=photo(999), sender_id=555), wr, thumb=False) == "pending"
    assert 23 in {r["msg_id"] for r in st.gallery_rows()}
    # 空名单恢复收全部
    w.reg.set_watch(CHAT, authors="")
    assert await w._take(CHAT, msg(24, photo=photo(1000), sender_id=666), wr2 := w.reg.watch(CHAT),
                         thumb=False) == "pending"
    # 把白名单测试塞进来的行清掉，别污染后面的相册/分组测试
    st.delete([23, 24])
    st.commit()

    # 话题监听：名单 654278+general；名单外的（无 reply=外话）和特定话题都不进。
    # 且：作者白名单和话题名单各管各的，都写了就都须过。
    topic654278 = types.SimpleNamespace(reply_to_top_id=None, reply_to_msg_id=654278)
    topic9 = types.SimpleNamespace(reply_to_top_id=None, reply_to_msg_id=555666)
    w.reg.set_watch(CHAT, kinds="photo,video", topics="654278,general")
    wr3 = w.reg.watch(CHAT)
    assert await w._take(CHAT, msg(31, photo=photo(2001), reply_to=topic654278), wr3,
                         thumb=False) == "pending"
    assert await w._take(CHAT, msg(32, photo=photo(2002), reply_to=topic9), wr3,
                         thumb=False) is None                     # 别的话题
    assert await w._take(CHAT, msg(33, photo=photo(2003)), wr3,
                         thumb=False) == "pending"                # General（无 reply 头）
    w.reg.set_watch(CHAT, topics="", authors="555")
    wr4 = w.reg.watch(CHAT)
    assert await w._take(CHAT, msg(34, photo=photo(2004), sender_id=555,
                                   reply_to=topic654278), wr4, thumb=False) == "pending"
    assert await w._take(CHAT, msg(35, photo=photo(2005), sender_id=666,
                                   reply_to=topic654278), wr4, thumb=False) is None
    # 话题名字复原成空，别污染后续测试
    w.reg.set_watch(CHAT, topics=None, authors=None)
    st.delete([31, 33, 34])
    st.commit()
    print("take     ok: 重发挂老文件、超限只记索引、贴纸不进库、类型过滤、作者白名单、话题监听、游标不动、重复看到不重复入库")

    # --- 4. 相册聚合 ---
    st.insert(row(CHAT, 10, grouped_id=999, status="done", size=1000, thumb="thumbs/a.jpg",
                  link="by-author/555/10.jpg", sha256="a" * 64, topic_id=654278))
    st.insert(row(CHAT, 11, grouped_id=999, status="pending", kind="video", size=2000,
                  caption="黑丝榨精腿", thumb="thumbs/b.jpg"))
    st.commit()
    groups = gallery.group_items(st.items(limit=50))
    alb = next(g for g in groups if g["grouped_id"] == 999)
    assert [m["msg_id"] for m in alb["members"]] == [10, 11], alb["members"]
    assert alb["kind"] == "album" and alb["kinds"] == ["photo", "video"], alb
    assert alb["status"] == "pending" and alb["caption"] == "黑丝榨精腿"
    assert alb["size"] == 3000 and alb["permalink"].endswith("/10"), alb
    assert alb["topic_title"] is None and alb["topic_id"] == 654278
    assert len(groups) == 4, [g["key"] for g in groups]     # 重复的第 2 条并进第 1 条那张卡
    g1 = next(g for g in groups if g["key"].endswith(":1"))
    assert [d["msg_id"] for d in g1["dups"]] == [2], g1["dups"]
    print(f"album    ok: 同 grouped_id 聚成一组（图+视频混合），状态取最差、说明取有caption的、"
          f"链接用第一条、重复的并进原件（{len(groups)} 张卡）")

    # --- 5. Range 解析（视频拖进度条靠它）---
    assert server.parse_range("bytes=0-99", 1000) == (0, 99)
    assert server.parse_range("bytes=500-", 1000) == (500, 999)
    assert server.parse_range("bytes=-100", 1000) == (900, 999)
    assert server.parse_range("bytes=0-99999", 1000) == (0, 999)
    assert server.parse_range("bytes=1000-", 1000) is None
    assert server.parse_range("bytes=5-1", 1000) is None
    assert server.parse_range("bytes=abc", 1000) is None
    assert server.parse_range("", 1000) is None
    assert server.parse_range("bytes=0-9", 0) is None
    print("range    ok: 闭区间/开区间/负数后缀都算对，越界和乱写退回整个文件")

    # --- 6. 路径穿越 ---
    root = os.path.join(d, "lib", str(CHAT))
    os.makedirs(os.path.join(root, "thumbs"), exist_ok=True)
    assert server.safe_lib_path(root, "thumbs/a.jpg") == os.path.join(root, "thumbs", "a.jpg")
    assert server.safe_lib_path(root, "../x") is None
    assert server.safe_lib_path(root, "a/../../x") is None
    assert server.safe_lib_path(root, "..") is None
    assert server.safe_lib_path(root, "") is None
    assert server.safe_lib_path(root, "C:/Windows/win.ini") is None
    assert server.safe_lib_path(root, "%2e%2e/x") is None
    print("path     ok: 只放行库目录内的相对路径，../ 和绝对路径都拒掉")

    # --- 7. 库：占用统计、增量查询、监视配置、磁盘上限 ---
    st2 = store.Store(d, -100999).open()
    st2.insert(row(-100999, 1, status="done", sha256="b" * 64, size=1000))
    st2.insert(row(-100999, 2, status="done", sha256="b" * 64, size=1000))
    st2.insert(row(-100999, 3, status="done", sha256="c" * 64, size=3000))
    st2.commit()
    assert st2.used_bytes() == 4000, st2.used_bytes()      # 同样的 sha 只算一次
    newest = st2.items(limit=1)[0]["msg_id"]
    assert st2.items(after_msg=newest) == []
    assert [r["msg_id"] for r in st2.items(before_msg=newest)] == [2, 1]
    # 排序键是消息号不是入库顺序：故意把一条老消息后插进来，它必须排在后面
    st2.insert(row(-100999, 0, status="done", sha256="d" * 64, size=500))
    st2.commit()
    assert [r["msg_id"] for r in st2.items()] == [3, 2, 1, 0]
    assert -100999 in store.chat_dirs(d)
    assert store.safe_name("a<b>:c/d\\e|f?g*h#i%j") == "a_b__c_d_e_f_g_h_i_j"
    print("store    ok: 占用按 sha 去重统计、增量/翻页查询、排序按消息号、群目录能扫出来、"
          "文件名消毒")

    reg = store.Registry(d).open()
    assert reg.disk_cap() == 50 << 30
    reg.set_setting("disk_cap_gb", 2)
    assert reg.disk_cap() == 2 << 30
    reg.set_watch(-100999, title="另一个群", kinds="photo", max_mb=50, enabled=True)
    assert reg.watch(-100999)["enabled"] == 1 and reg.watch(-100999)["max_mb"] == 50
    assert -100999 in {r["chat_id"] for r in reg.enabled()}
    reg.set_watch(-100999, enabled=False)
    assert -100999 not in {r["chat_id"] for r in reg.enabled()}
    assert reg.watch(-100999)["started_at"] is None
    print("watch    ok: 开始/停止会记 started_at，磁盘上限可改（默认 50 GB）")

    # --- 7.5 配置：环境变量优先、存在库里、明文不外传 ---
    cfg = store.Config(d)
    assert not cfg.ready and cfg.api_id is None and not cfg.api_hash_set
    assert cfg.api_hash == store.Config.PLACEHOLDER_HASH        # 没填 hash 也建得出客户端
    cfg.save(12345678, "f" * 32)
    assert cfg.ready and cfg.api_id == 12345678 and cfg.api_hash == "f" * 32
    assert store.Config(d).api_hash == "f" * 32                 # 落库了，重开还在
    os.environ["TG_API_ID"] = "999"
    os.environ["TG_API_HASH"] = "e" * 32
    try:
        assert store.Config(d).api_id == 999                    # 环境变量优先
        assert store.Config(d).api_hash == "e" * 32
    finally:
        del os.environ["TG_API_ID"]
        del os.environ["TG_API_HASH"]
    assert store.Config(d).api_id == 12345678

    w2 = watcher.Watcher.__new__(watcher.Watcher)
    w2.cfg, w2.state, w2.error, w2.client, w2.note = cfg, "ok", None, None, ""
    w2.session_file = os.path.join(d, "x.session")
    w2.reg = reg                       # status() 要读「有没有设访问密码」
    w2.locked, w2.blob_session = False, None
    st_json = json.dumps(await w2.status(), ensure_ascii=False)
    assert '"api_id": 12345678' in st_json and '"api_hash_set": true' in st_json
    assert '"locked": false' in st_json and '"passcode_set": false' in st_json
    assert "f" * 32 not in st_json, "api_hash 明文绝不能出现在给网页的响应里"
    assert "api_hash\"" not in st_json, st_json
    assert w2.login_ready() is None, "id 和 hash 都齐了，重新登录该放行"

    # 只填 id、不填 hash：能连（hash 只在登录时用），但重新登录必须先把 hash 补上
    bare_root = os.path.join(d, "bare")
    store.Config(bare_root).save(87654321, None)
    bare = store.Config(bare_root)
    assert bare.ready and bare.api_id == 87654321 and not bare.api_hash_set
    assert bare.api_hash == store.Config.PLACEHOLDER_HASH
    w3 = watcher.Watcher.__new__(watcher.Watcher)
    w3.cfg = bare
    assert w3.login_ready(), "没填 api_hash 就不该放行重新登录"
    w3.cfg = store.Config(os.path.join(d, "nothing"))
    assert w3.login_ready() == "还没填 api_id"
    print("config   ok: 环境变量 > 库、重开还在、明文不外传；没填 hash 能连但不能重新登录")

    # 被系统挂起的检测：睡过头的那部分才算挂起，正常睡满不算
    assert watcher.frozen_seconds(100.0, 8, 108.0) == 0.0
    assert watcher.frozen_seconds(100.0, 8, 145.0) == 37.0
    assert watcher.frozen_seconds(100.0, 2, 101.0) == 0.0
    print("frozen   ok: 睡过头才算被挂起（正常睡满不算），差值就是挂起时长")

    # --- 7.6 补历史：绝不碰游标 ---
    class BackfillTG:
        def __init__(self, msgs):
            self.msgs = msgs
            self.pages = 0

        async def __call__(self, req):
            self.pages += 1
            pool = sorted([m for m in self.msgs if req.offset_id == 0 or m.id < req.offset_id],
                          key=lambda m: -m.id)
            return types.SimpleNamespace(messages=pool[:req.limit])

        async def get_input_entity(self, chat_id):
            return "peer"

        async def download_media(self, msg, file=None, thumb=None):
            return None

    bf_chat = -100777
    bf = watcher.Watcher.__new__(watcher.Watcher)
    bf.root, bf.reg, bf.stores, bf.note = d, store.Registry(d).open(), {}, ""
    bf.client = BackfillTG([msg(1000 + i, photo=photo(2000 + i),
                                date=NOW - timedelta(minutes=7 * i)) for i in range(1, 20)])
    bf.session_file = os.path.join(d, "x.session")
    added = await bf.backfill(bf_chat, hours=1)
    bst = bf.store(bf_chat)
    assert added == 8, f"1 小时 = 60 分钟，7/14/…/56 分钟前那 8 条该收下，实际 {added}"
    assert bst.cursor() == 0, "补历史绝不能推进游标，否则兜底扫描的缺口保护就废了"
    assert len(bst.gallery_rows()) == 8
    assert bf.client.pages == 1, f"一页就翻到时间线了，实际翻了 {bf.client.pages} 页"
    print("backfill ok: 按时间线补 8 条、撞线就停，游标一动不动（缺口保护还在）")

    # --- 8. 静态离线导出 ---
    st.insert(row(CHAT, 20, status="done", thumb="thumbs/c.jpg", link="by-author/555/20.jpg",
                  caption="<b>尖括号</b>&", grouped_id=888))
    st.insert(row(CHAT, 21, status="done", kind="video", thumb="thumbs/d.jpg",
                  link="by-author/555/21.mp4", grouped_id=888))
    st.insert(row(CHAT, 22, status="skipped", size=700 * MiB, note="超过 100 MB"))
    st.commit()
    html_path = os.path.join(d, "index.html")
    n = gallery.render(html_path, st.gallery_rows(), "测试群", CHAT)
    txt = open(html_path, encoding="utf-8").read()
    assert n == 6, n                     # 重复的那条并进原件，不单独成卡
    assert 'id="q"' in txt and "<script>" in txt and "<video" in txt
    assert "&lt;b&gt;尖括号&lt;/b&gt;&amp;" in txt and "<b>尖括号" not in txt
    assert "共 2 个" in txt and "超过 100 MB" in txt and "重复 ×2" in txt
    print(f"offline  ok: 静态导出 {n} 张卡，相册合成一张（图+视频），caption 转义，视频用 <video>")

    # --- 9. 目标解析 / 永久链接 / 话题归属 ---
    assert tg.parse_target("-1001234567890") == CHAT
    assert tg.parse_target("1234567890") == CHAT
    assert tg.parse_target("t.me/c/1234567890/684345") == CHAT
    assert tg.parse_target("@somegroup") == "@somegroup"
    assert tg.proxy_dict("socks5h://127.0.0.1:10808") == {
        "proxy_type": "socks5", "addr": "127.0.0.1", "port": 10808, "rdns": True}
    assert tg.permalink_id(CHAT, 5, 654278) == "https://t.me/c/1234567890/654278/5"
    assert tg.permalink_id(CHAT, 5) == "https://t.me/c/1234567890/5"
    assert tg.permalink_id(-123456, 5) is None                  # 基础群没有这种链接
    assert tg.topic_of(msg(1)) is None
    assert tg.topic_of(msg(1, reply_to=types.SimpleNamespace(
        reply_to_top_id=None, reply_to_msg_id=654278))) == 654278
    assert tg.topic_of(msg(1, reply_to=types.SimpleNamespace(
        reply_to_top_id=684058, reply_to_msg_id=684064))) == 684058
    print("target   ok: id 三种写法归一、链接带话题号、基础群不给链接、话题优先 top 再退 msg")

    # --- 9.5 编辑 / 删除：编辑不改消息 ID，必须是原地更新 ---
    w.reg.set_watch(CHAT, kinds="photo,video", enabled=True)
    wr = w.reg.watch(CHAT)
    n_before = len(st.gallery_rows())
    await w._revise(CHAT, msg(1, photo=photo(777), message="改了说明",
                              edit_date=NOW + timedelta(minutes=5)), wr)
    r1 = st.get(1)
    assert r1["caption"] == "改了说明" and r1["edited"], r1["caption"]
    assert len(st.gallery_rows()) == n_before, "编辑必须原地更新，不能多出一行"

    # 编辑把媒体换掉：回到 pending 重新下，但**旧文件先留着** —— 新版下好之前卡片还得能点开
    st.set_status(1, "done", None, sha256="b" * 64, path="files/bb/x.jpg",
                  link="by-author/555/1.jpg")
    st.commit()
    blob_old = st.abs("files/bb/x.jpg")
    os.makedirs(os.path.dirname(blob_old), exist_ok=True)
    open(blob_old, "wb").write(b"o" * 300)
    swapped = msg(1, document=doc(999, "video/mp4", 3 * MiB, video=(720, 1280, 9)),
                  edit_date=NOW + timedelta(minutes=6))
    assert await w._revise(CHAT, swapped, wr) == "pending"
    r1 = st.get(1)
    assert (r1["kind"], r1["file_uid"], r1["status"]) == ("video", 999, "pending"), dict(r1)
    assert r1["path"] == "files/bb/x.jpg" and r1["sha256"] == "b" * 64, \
        "新版没下好之前，旧文件要留着，卡片才点得开"
    assert r1["caption_before"] == "改了说明", "改之前的说明要记下来给「已编辑」展开看"

    # 新版下好：旧文件**不再被清**，而是留下一条历史版本 —— 「编辑不删内容」
    st.set_done(1, "e" * 64, "files/ee/new.mp4", None, None)
    st.commit()
    r1 = st.get(1)
    assert r1["path"] == "files/ee/new.mp4" and r1["status"] == "done", dict(r1)
    assert os.path.exists(blob_old), "编辑换掉的旧文件必须留着（卡片要显示原图）"
    revs = st.revisions(1)
    assert len(revs) == 1 and revs[0]["path"] == "files/bb/x.jpg", [dict(x) for x in revs]

    # 编辑把媒体删掉：索引留着（归档的就是当时发出来的东西），只更新文字
    await w._revise(CHAT, msg(1, message="删了媒体",
                              edit_date=NOW + timedelta(minutes=7)), wr)
    r1 = st.get(1)
    assert r1["caption"] == "删了媒体" and "媒体已不在" in (r1["note"] or ""), r1["note"]

    # 再编辑一次换媒体：历史版本**累加**，不是覆盖（多次编辑也要有）
    st.set_status(1, "done", None, sha256="e" * 64, path="files/ee/new.mp4",
                  link="by-author/555/1.mp4")
    st.commit()
    blob_mid = st.abs("files/ee/new.mp4")
    os.makedirs(os.path.dirname(blob_mid), exist_ok=True)
    open(blob_mid, "wb").write(b"m" * 300)
    await w._revise(CHAT, msg(1, photo=photo(1000),
                              edit_date=NOW + timedelta(minutes=8)), wr)
    assert len(st.revisions(1)) == 2, [dict(x) for x in st.revisions(1)]
    assert os.path.exists(blob_old) and os.path.exists(blob_mid), "两份旧媒体都还在盘上"

    # 被编辑过的行永远不算「重复」：内容撞车不是重发（参考项目按 file_uid 去重）
    st.set_done(1, "b" * 64, "files/bb/x.jpg", None, None, dup_of=9)
    st.commit()
    assert st.get(1)["dup_of"] is None, "被编辑过的行不能被标成重复"

    # 删卡片：历史版本连文件一起清掉（到这一刻才真删）
    st.delete([1])
    assert not st.revisions(1), "删卡片要连历史版本一起删"
    assert not os.path.exists(blob_mid), "历史版本独占的文件跟着删"
    assert os.path.exists(blob_old), "还有别的行指着同一份文件时不能删（引用计数说了算）"

    st.mark_deleted(5)
    first = st.get(5)["deleted"]
    assert first, "删除要打标记"
    st.mark_deleted(5)
    assert st.get(5)["deleted"] == first, "重复标记不该改时间"
    print("revise   ok: 编辑原地更新、换媒体的旧文件永不删（历史版本，多次编辑累加）、"
          "记下改前说明、被编辑的行不算重复、删卡片才连历史版本一起清、删媒体只更新文字、删除打标记")

    # 迟到的编辑事件（edit_date 早于 added = 入库前就发生的编辑，待机恢复回放常见）：
    # 只推 edit_seen 水位 —— 不标「已编辑」、不留没有文件的历史版本（线上 699714 的假卡）
    st.insert(row(CHAT, 6, status="done", sha256="f" * 64, path="files/ff/x.jpg",
                  thumb="thumbs/6.jpg", file_uid=600,
                  added=int((NOW + timedelta(minutes=2)).timestamp())))
    st.commit()
    await w._revise(CHAT, msg(6, document=doc(601, "video/mp4", 3 * MiB, video=(720, 1280, 9)),
                              edit_date=NOW + timedelta(minutes=1)), wr)
    r6 = st.get(6)
    assert r6["edited"] is None and r6["file_uid"] == 600, dict(r6)
    assert not st.revisions(6), "入库前的编辑换媒体也不该留版本"
    assert r6["edit_seen"] == int((NOW + timedelta(minutes=1)).timestamp())
    st.delete([6])
    st.commit()

    # --- 9.6 老库迁移：补列、留备份、不动数据 ---
    import sqlite3
    old_d = store.dirs(d, -1)
    os.makedirs(os.path.dirname(old_d["db"]), exist_ok=True)
    old_schema = store.SCHEMA.replace(
        "added INTEGER DEFAULT 0, edited INTEGER, deleted INTEGER, star INTEGER,",
        "added INTEGER DEFAULT 0,")
    con = sqlite3.connect(old_d["db"])
    con.executescript(old_schema)
    before = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    assert not ({"edited", "deleted", "star"} & before), \
        "测试前提：造出来的老库确实没有这三列，实际 " + str(sorted(before))
    con.execute("INSERT INTO messages(chat_id,msg_id,date,kind,status,caption) "
                "VALUES(?,?,?,?,?,?)", (-1, 7, 1, "photo", "done", "老数据"))
    con.commit()
    con.close()
    st_old = store.Store(d, -1).open()
    cols = {r["name"] for r in st_old.con.execute("PRAGMA table_info(messages)")}
    assert {"edited", "deleted", "star"} <= cols, sorted(cols)
    assert st_old.get(7)["caption"] == "老数据", "迁移不能动数据"
    assert os.path.exists(old_d["db"] + ".bak"), "迁移前要留一份备份"
    st_old.close()
    print("migrate  ok: 老库补列、留了 index.db.bak、原有数据一行没动")

    # --- 9.7 删除：共享文件按引用计数删 ---
    dchat = -100555
    dstore = store.Store(d, dchat).open()
    blob = os.path.join(dstore.d["base"], "files", "aa", "shared.jpg")
    os.makedirs(os.path.dirname(blob), exist_ok=True)
    open(blob, "wb").write(b"x" * 500)
    thumb = os.path.join(dstore.d["base"], "thumbs", "77.jpg")
    os.makedirs(os.path.dirname(thumb), exist_ok=True)
    open(thumb, "wb").write(b"t" * 10)
    link_rel_ = "by-author/555/20260101_1_photo_aaaaaaaa.jpg"
    link_abs = os.path.join(dstore.d["base"], "by-author", "555",
                            "20260101_1_photo_aaaaaaaa.jpg")
    os.makedirs(os.path.dirname(link_abs), exist_ok=True)
    open(link_abs, "wb").write(b"x" * 500)
    dstore.insert(row(dchat, 1, status="done", sha256="a" * 64, size=500,
                      path="files/aa/shared.jpg", link=link_rel_,
                      thumb="thumbs/77.jpg", file_uid=77))
    dstore.insert(row(dchat, 2, status="done", sha256="a" * 64, size=500,
                      path="files/aa/shared.jpg", thumb="thumbs/77.jpg", file_uid=77))
    dstore.commit()
    out = dstore.delete([1])
    assert out["deleted"] == 1 and out["bytes"] == 0, out
    assert os.path.exists(blob) and os.path.exists(thumb), "还有别的条目指着它，真身不能删"
    assert not os.path.exists(link_abs), "硬链接该删掉"
    out = dstore.delete([2])
    assert out["bytes"] == 500, out
    assert not os.path.exists(blob) and not os.path.exists(thumb), "最后一条走了，真身才删"
    assert dstore.gallery_rows() == []
    print("delete   ok: 删索引+硬链接，真身和缩略图按引用计数、最后一条走了才删文件")

    # --- 9.8 作者聚合 ---
    dstore.insert(row(dchat, 3, author_id=555, status="done", sha256="b" * 64, size=100))
    dstore.insert(row(dchat, 4, author_id=555, status="done", sha256="b" * 64, size=100))
    dstore.insert(row(dchat, 5, author_id=666, status="skipped", size=700))
    dstore.author_dir(555, "某某")                        # 作者目录行是真下载时才建的（真流程）
    dstore.author_dir(666, None)
    dstore.commit()
    au = {a["author_id"]: a for a in dstore.authors()}
    assert au[555]["n"] == 2 and au[555]["bytes"] == 100, au[555]     # 同一个 sha 只算一次
    assert au[666]["n"] == 1 and au[666]["bytes"] == 0, au[666]       # 没下文件就不占空间
    assert [r["msg_id"] for r in dstore.items(author=666)] == [5]
    pre = [a["author_id"] for a in dstore.authors()]                 # 按名称排序：
    dstore.delete([3, 4, 5])                              # 全删光后也不许从列表里消失
    dstore.commit()
    au = {a["author_id"]: a for a in dstore.authors()}
    assert len(au) == 2 and au[555]["n"] == 0 and au[555]["bytes"] == 0, au
    assert au[666]["n"] == 0, au[666]
    assert [a["author_id"] for a in dstore.authors()] == pre, "删改前后同一作者的位次不变（滚轮邻居稳定）"
    dstore.close()
    print("authors  ok: 条数/占用按 sha 去重、跳过的不算占用、能按作者筛")

    # --- 9.9 本机识别 ---
    assert server.is_local("127.0.0.1") and server.is_local("::1")
    assert not server.is_local("192.168.1.50"), "别的机器不能算本机"
    print("local    ok: 回环是本机、局域网里别人不是")

    # --- 9.9b 在资源管理器中显示：命令行必须给路径加引号 ---
    # 不加引号时 list2cmdline 只在有空格时才补，explorer 解析 /select, 失败就退化成打开「文档」
    calls = []
    real_popen, server.subprocess.Popen = server.subprocess.Popen, lambda cmd: calls.append(cmd)
    try:
        server.show_in_explorer(r"D:\a\b c\x.jpg")
        server.show_in_explorer(r"D:\a\bc\x.jpg")
    finally:
        server.subprocess.Popen = real_popen
    assert calls[0] == 'explorer /select,"D:\\a\\b c\\x.jpg"', calls[0]
    assert calls[1] == 'explorer /select,"D:\\a\\bc\\x.jpg"', calls[1]
    print("explorer ok: 路径一律加引号（有没有空格都加），explorer 才会选中那个文件")

    # --- 9.10 编辑：只留最新版，而且只有我们显示的东西真的变了才算编辑 ---
    vchat = -100666
    vst = w.store(vchat)                     # 必须和监听器用同一个实例，否则读到旧事务快照
    blob1 = os.path.join(vst.d["base"], "files", "cc", "one.jpg")
    os.makedirs(os.path.dirname(blob1), exist_ok=True)
    open(blob1, "wb").write(b"y" * 800)
    vst.insert(row(vchat, 1, status="done", sha256="c" * 64, size=800, caption="原始说明",
                   path="files/cc/one.jpg", thumb="thumbs/v1.jpg", file_uid=901))
    vst.commit()
    w.reg.set_watch(vchat, kinds="photo,video", enabled=True)
    vw = w.reg.watch(vchat)
    same = photo(901)                        # 同一个 file_uid：媒体没换
    t1 = NOW + timedelta(minutes=3)

    # ① 说明和媒体都没变（Telegram 也会为改内联按钮之类发编辑更新）：不动、不标「已编辑」
    await w._revise(vchat, msg(1, photo=same, message="原始说明", edit_date=t1), vw)
    vrow = vst.get(1)
    assert vrow["caption"] == "原始说明" and not vrow["edited"], dict(vrow)
    assert vrow["edit_seen"] == int(t1.timestamp()), "水位要推上去，否则每轮扫描都白忙"
    assert vst.used_bytes() == 800

    # ② 归档之前就带着 edit_date 的消息（作者早改过）不算新编辑
    added = int(t1.timestamp())
    assert not watcher.is_new_edit({"edit_seen": None, "added": added + 60},
                                   msg(1, edit_date=t1)), "归档前的编辑时间不算新编辑"
    assert watcher.is_new_edit({"edit_seen": None, "added": added - 60},
                               msg(1, edit_date=t1)), "归档之后改的才算"
    assert not watcher.is_new_edit({"edit_seen": added, "added": added - 60},
                                   msg(1, edit_date=t1)), "处理过的那次不重复处理"
    assert not watcher.is_new_edit(None, msg(1, edit_date=t1)), "库里没这条就别动"

    # ③ 只改说明：原地更新
    await w._revise(vchat, msg(1, photo=same, message="改过的说明",
                               edit_date=t1 + timedelta(minutes=1)), vw)
    vrow = vst.get(1)
    assert vrow["caption"] == "改过的说明" and vrow["edited"], dict(vrow)
    assert os.path.exists(blob1) and vst.used_bytes() == 800

    # ④ 换媒体：主行换成新的、重新排队下载；旧文件先留着（卡片还能点开），新版下好才清
    await w._revise(vchat, msg(1, document=doc(902, "video/mp4", 2 * MiB, video=(720, 1280, 5)),
                               edit_date=t1 + timedelta(minutes=2)), vw)
    vrow = vst.get(1)
    assert vrow["kind"] == "video" and vrow["file_uid"] == 902, dict(vrow)
    assert vrow["status"] == "pending", "换了媒体要重新下"
    assert os.path.exists(blob1) and vrow["path"] == "files/cc/one.jpg", \
        "新版没下好之前旧文件留着，卡片才点得开"
    assert vrow["caption_before"] == "改过的说明", vrow["caption_before"]
    vst.set_done(1, "f" * 64, "files/ff/new.mp4", None, None)
    vst.commit()
    assert os.path.exists(blob1), "换掉的旧媒体要留着（历史版本，卡片显示原图）"
    assert len(vst.revisions(1)) == 1, [dict(x) for x in vst.revisions(1)]
    assert vst.used_bytes() > 800, "历史版本也要算进磁盘占用"

    # 显示契约：卡片主图必须是**原图**，现在那份挂在 now 上，版本条按 seq 列出来
    import gallery as _gal
    gm = _gal.group_items(vst.items(limit=50), {1: vst.revisions(1)})[0]["members"][0]
    assert gm["kind"] == "photo" and gm["file"] == "files/cc/one.jpg", \
        f"编辑过的卡片要显示原图，实际 {gm['kind']} {gm['file']}"
    assert gm["now"]["kind"] == "video", "现在那份要挂在 now 上"
    assert len(gm["revs"]) == 1 and gm["revs"][0]["seq"] == 1, [dict(x) for x in gm["revs"]]

    # --- 9.10a 导出 / 打包：跟当前筛选一致，而且带上历史版本 ---
    h = server.Handler.__new__(server.Handler)     # 不启 HTTP，只借那两个方法
    h.app = types.SimpleNamespace(watcher=w)

    def csv_lines(**q):
        raw = h._csv_bytes(dict({"chat": str(vchat)}, **q))["raw"].lstrip("\ufeff")
        return list(csv.reader(io.StringIO(raw)))

    lines = csv_lines()
    assert lines[0][7] == "版本", lines[0]
    assert len(lines) == 3, lines                     # 表头 + 现在 + 一条历史版本，各占一行
    assert [r[7] for r in lines[1:]] == ["原图", "现在"], [r[7] for r in lines[1:]]
    assert lines[1][11].endswith(os.sep + "one.jpg"), lines[1][11]
    assert lines[2][11].endswith(os.sep + "new.mp4"), lines[2][11]
    assert lines[1][4] == "photo" and lines[2][4] == "video", "每一版写自己的类型和路径"
    assert lines[1][9] == "改过的说明", "每一版写自己的说明"

    # 筛选：条目按**当前**类型算（跟列表一致），历史版本跟着它一起导出
    assert len(csv_lines(kinds="video")) == 3 and len(csv_lines(kinds="photo")) == 1
    assert len(csv_lines(author="555")) == 3 and len(csv_lines(author="999")) == 1
    assert vst.count() == 1 and vst.count(kinds="photo") == 0 and vst.count(author="999") == 0

    # 打包：历史版本进 history/<消息号>/，主文件还是原来的相对路径
    blob2 = os.path.join(vst.d["base"], "files", "ff", "new.mp4")
    os.makedirs(os.path.dirname(blob2), exist_ok=True)
    open(blob2, "wb").write(b"v" * 900)
    z = h._zip({"chat": str(vchat)})
    assert z["n"] == 1 and z["hist"] == 1, z
    with zipfile.ZipFile(z["file"]) as zf:
        names = zf.namelist()
    assert "files/ff/new.mp4" in names, names
    assert any(n.startswith("history/1/1-") for n in names), names
    assert "files/cc/one.jpg" not in names, "历史版本走 history/，别跟主文件混在一起"
    os.remove(z["file"])
    assert h._zip({"chat": str(vchat), "kinds": "photo"})["n"] == 0, "打包也要认筛选"

    # 静态离线页：和网页端一样有「已编辑」版本条，每一版点开就是那个文件
    off = os.path.join(d, "offline-rev.html")
    gallery.render(off, vst.gallery_rows(), "编辑过的群", vchat, {1: vst.revisions(1)})
    otxt = open(off, encoding="utf-8").read()
    assert '<details class="revs"><summary>已编辑</summary>' in otxt
    assert "revstrip" in otxt and ">原图<" in otxt and ">现在<" in otxt, "版本条要有原图和现在"
    assert "one.jpg" in otxt and "new.mp4" in otxt, "离线页要能点开每一版"

    # 原图没文件（迟到编辑换媒体的典型：原版还没下载就被换掉，线上 699714 那两张卡）：
    # 版本里一份有文件的都没有 → 主图回落到当前那份，下好了的卡片不再挂着「排队中」
    vst.insert(row(vchat, 2, status="done", sha256="9" * 64, kind="photo", size=700,
                   path="files/ff/orig2.jpg", thumb="thumbs/z.jpg", file_uid=701,
                   caption="", added=int(t1.timestamp()), edited=int(t1.timestamp())))
    vst.commit()
    vst.con.execute(
        "INSERT INTO revisions(chat_id,msg_id,seq,kind,size,mime,width,height,"
        "caption,sha256,path,thumb,edited_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (vchat, 2, 1, "photo", 600, "image/jpeg", 100, 200, None, None, None,
         "thumbs/z1.jpg", int(t1.timestamp())))
    vst.commit()
    gm = _gal.group_items(vst.items(limit=50), {2: vst.revisions(2)})[0]["members"][0]
    assert gm["file"] == "files/ff/orig2.jpg", gm["file"]
    assert gm["kind"] == "photo" and gm["thumb"] == "thumbs/z.jpg", dict(gm)
    assert len(gm["revs"]) == 1, [dict(x) for x in gm["revs"]]
    vst.close()
    print("export   ok: 清单/打包跟当前筛选一致，每个版本各占一行，zip 里历史版本进 history/，"
          "静态离线页也有版本条")
    print("edit     ok: 内容没变不算编辑、归档前的编辑时间不算、改说明原地更新、"
          "换媒体的旧文件永久保留并记成历史版本、记下改前说明")

    # --- 9.10b 重复内容：同一个文件重发不单独成卡，挂到原件那张卡上 ---
    rchat = -1000000888888                  # -100 + 888888：permalink_id 才拼得出链接
    rst = w.store(rchat)
    rst.insert(row(rchat, 10, status="done", sha256="f" * 64, size=900, file_uid=5000,
                   thumb="thumbs/a.jpg"))
    rst.insert(row(rchat, 11, status="done", sha256="f" * 64, size=900, file_uid=5000,
                   dup_of=10, thumb="thumbs/a.jpg"))
    rst.insert(row(rchat, 12, status="pending", file_uid=6000))
    rst.commit()
    groups = gallery.group_items(rst.gallery_rows())
    by_msg = {g["members"][0]["msg_id"]: g for g in groups}
    assert sorted(by_msg) == [10, 12], sorted(by_msg)        # 11 被并进 10 那张卡
    g10 = by_msg[10]
    assert [d["msg_id"] for d in g10["dups"]] == [11], g10["dups"]
    assert g10["dups"][0]["permalink"], "重复那条的原消息链接要留着"
    assert g10["size"] == 900, "重复的那份不该把体积算两遍"
    rst.close()
    print("dupmerge ok: 重复条目并进原件那张卡、原消息链接保留、体积不算两遍")

    # --- 9.10b2 一个相册里好几条原件各带重复行：一条都不能从画廊里消失 ---
    # （线上真踩到过：g["dups"] 写成覆盖而不是累加，7 条真消息凭空不见了）
    ach = -1000000444444
    ast = w.store(ach)
    for mid in (20, 21):                       # 同一个相册的两条原件
        ast.insert(row(ach, mid, status="done", sha256=str(mid) * 64, size=500,
                       grouped_id=9001, thumb=f"thumbs/{mid}.jpg"))
        ast.insert(row(ach, mid + 100, status="done", sha256=str(mid) * 64, size=500,
                       grouped_id=9001, dup_of=mid, thumb=f"thumbs/{mid}.jpg"))
    ast.commit()
    agroups = gallery.group_items(ast.gallery_rows())
    assert len(agroups) == 1, [g["key"] for g in agroups]
    got = sorted(d["msg_id"] for d in agroups[0]["dups"])
    assert got == [120, 121], got
    seen = {m["msg_id"] for m in agroups[0]["members"]} | set(got)
    assert seen == {20, 21, 120, 121}, "画廊里的行 + 重复挂载必须等于库里的全部行"
    ast.close()
    print("dupalbum ok: 一个相册多条原件各带重复行时全都留得住（覆盖写成累加那个 bug）")

    # --- 9.10c 下载退避：没到点的先不挑，到点了接着挑（永不放弃）---
    bchat = -100889
    bst = w.store(bchat)
    bst.insert(row(bchat, 20, status="pending", file_uid=7000))
    bst.insert(row(bchat, 21, status="pending", file_uid=7001,
                   next_try=int(NOW.timestamp()) + 300))
    bst.commit()
    assert [r["msg_id"] for r in bst.pending("full")] == [20], "退避没到点的先不挑"
    later = int(NOW.timestamp()) + 600
    assert [r["msg_id"] for r in bst.pending("full", now=later)] == [20, 21], "到点了接着挑"
    # 进度：tmp 里的 .part 按 file_uid 反查得到是哪条消息、下到多少
    with open(os.path.join(bst.d["tmp"], "7000.part"), "wb") as f:
        f.write(b"q" * 1234)
    bst.set_status(20, "pending", None, size=5000)
    bst.commit()
    prog = await w.downloading()
    assert prog["20"] == {"done": 1234, "total": 5000}, prog
    bst.close()
    print("backoff  ok: 退避没到点的先不挑、到点了接着挑，不会永久放弃；进度按 .part 反查")

    # --- 9.10d 老库的历史版本表：只留最新版，一次清干净 ---
    mchat = -100890
    mst = w.store(mchat)
    shared = os.path.join(mst.d["base"], "files", "dd", "shared.jpg")
    only = os.path.join(mst.d["base"], "files", "dd", "only.jpg")
    linkp = os.path.join(mst.d["base"], "by-author", "555", "30.jpg")
    os.makedirs(os.path.dirname(shared), exist_ok=True)
    os.makedirs(os.path.dirname(linkp), exist_ok=True)
    open(shared, "wb").write(b"z" * 100)
    open(only, "wb").write(b"z" * 200)
    open(linkp, "wb").write(b"z" * 100)
    mst.insert(row(mchat, 30, status="done", sha256="1" * 64, size=100,
                   path="files/dd/shared.jpg", link="by-author/555/30.jpg"))
    mst.con.execute(
        "CREATE TABLE versions (chat_id INTEGER, msg_id INTEGER, version INTEGER, kind TEXT, "
        "size INTEGER, mime TEXT, caption TEXT, file_uid INTEGER, sha256 TEXT, path TEXT, "
        "link TEXT, thumb TEXT, status TEXT, note TEXT, edited INTEGER, replaced INTEGER, "
        "permalink TEXT, grouped_id INTEGER, width INTEGER, height INTEGER, duration REAL)")
    # v1 和主行同一份内容（硬链接同名），v2 是换掉的旧媒体（独占那份文件）
    for ver, uid, sha, path, link in ((1, 1234, "1" * 64, "files/dd/shared.jpg",
                                       "by-author/555/30.jpg"),
                                      (2, 1235, "2" * 64, "files/dd/only.jpg",
                                       "by-author/555/old.jpg")):
        mst.con.execute(
            "INSERT INTO versions(chat_id,msg_id,version,size,file_uid,sha256,path,link,status) "
            "VALUES(?,?,?,?,?,?,?,?,'done')", (mchat, 30, ver, 100 * ver, uid, sha, path, link))
    mst.con.commit()
    mst.close()
    mst = store.Store(d, mchat).open()          # 重新打开：迁移该把这张表清掉
    assert not mst.con.execute("SELECT 1 FROM sqlite_master WHERE name='versions'").fetchone(), \
        "历史版本表该被清掉"
    assert os.path.exists(shared), "主行还指着这份文件，不能删"
    assert os.path.exists(linkp), "和主行同名的那份硬链接不能被旧版清理带走"
    assert not os.path.exists(only), "只有历史版本指着的旧文件要清掉"
    mst.close()
    print("verclean ok: 老库的历史版本表被清掉，和主行共用的文件留着、独占的旧文件清掉")

    # --- 9.10e 同一个文件在别处已经下好了、这条却挂着失败：挂到那条上 ---
    hchat = -1000000999999
    hst = w.store(hchat)
    hst.insert(row(hchat, 40, status="done", sha256="9" * 64, size=700, file_uid=8888,
                   path="files/ee/x.mp4", thumb="thumbs/8888.jpg"))
    hst.insert(row(hchat, 41, status="failed", note="消息已经不在了（被删或撤回）", file_uid=8888))
    hst.commit()
    hst.close()
    hst = store.Store(d, hchat).open()          # 重新打开时自愈
    r41 = hst.get(41)
    assert r41["status"] == "done" and r41["dup_of"] == 40, dict(r41)
    assert r41["path"] == "files/ee/x.mp4" and r41["note"] is None, dict(r41)
    hgroups = gallery.group_items(hst.items(limit=50))
    assert len(hgroups) == 1 and [x["msg_id"] for x in hgroups[0]["dups"]] == [41], hgroups
    hst.close()
    print("heal     ok: 同一个文件别处下好了，失败/重复那条自动挂上去，不再多一张失败卡")

    # --- 9.10f 互相指的环 + 旧的「已编辑」误判标 ---
    cchat = -1000000777777
    cst = w.store(cchat)
    cst.insert(row(cchat, 50, status="done", sha256="7" * 64, size=600, file_uid=9100,
                   path="files/ff/a.jpg", dup_of=51))
    cst.insert(row(cchat, 51, status="done", sha256="7" * 64, size=600, file_uid=9100,
                   path="files/ff/a.jpg", dup_of=50))
    cst.insert(row(cchat, 52, status="done", sha256="8" * 64, size=600, edited=NOW))
    cst.commit()
    cst.close()
    cst = store.Store(d, cchat).open()
    # 「旧的已编辑标清一次」是一次性的（meta 里记了标记），这里把它抹掉，装成还没清过的老库
    cst.con.execute("DELETE FROM meta WHERE key='edited_reset'")
    cst.commit()
    cst.close()
    cst = store.Store(d, cchat).open()          # 重新打开：破环 + 清掉旧的「已编辑」标
    assert cst.get(50)["dup_of"] is None and cst.get(51)["dup_of"] == 50, \
        [(r["msg_id"], r["dup_of"]) for r in cst.gallery_rows()]
    assert cst.get(52)["edited"] is None, "旧的「已编辑」误判标要清掉"
    cgroups = gallery.group_items(cst.gallery_rows())
    assert len(cgroups) == 2, [g["key"] for g in cgroups]      # 50+51 一张，52 一张
    g50 = next(g for g in cgroups if g["members"][0]["msg_id"] == 50)
    assert [x["msg_id"] for x in g50["dups"]] == [51], g50["dups"]
    # 就算库里还留着环（从更老的库恢复出来），画廊也不能把两张卡一起丢掉
    ring = [dict(r, dup_of=51 if r["msg_id"] == 50 else 50) for r in cst.gallery_rows()
            if r["msg_id"] in (50, 51)]
    assert len(gallery.group_items(ring)) == 1, "环上也要落回同一张卡，不能谁都不是原件"
    cst.close()
    print("dupring  ok: 互相指的环破掉（早发的那条当原件）、旧「已编辑」标清掉、环上不丢卡")

    # --- 9.10h 下载：一条下不动绝不能把整轮带走（这次线上 598 次报错就是这里漏的）---
    class DlTG:
        """假的客户端：第 1 条怎么都下不动，第 2、3 条正常。"""

        def __init__(self):
            self.refetched = 0
            self.got = []

        async def get_input_entity(self, chat_id):
            return "peer"

        async def get_messages(self, peer, ids):
            mid = ids[0] if isinstance(ids, (list, tuple)) else ids
            return [msg(mid, photo=photo(9000 + mid))]

        async def iter_download(self, m, offset=0, request_size=None):
            if m.id == 1:
                raise TypeError("Cannot cast MessageMediaWebPage to any kind of InputFileLocation.")
            if m.id == 2 and not self.refetched:
                self.refetched += 1
                raise watcher.FileReferenceExpiredError(request=None)   # 引用过期一次
            self.got.append((m.id, offset))
            yield b"z" * 10

    dchat = -1000000666666
    dst = w.store(dchat)
    for mid, size in ((1, 10), (2, 10), (3, 10)):
        dst.insert(row(dchat, mid, status="pending", size=size, file_uid=8000 + mid))
    dst.commit()
    dw = watcher.Watcher.__new__(watcher.Watcher)
    dw.root, dw.reg, dw.stores, dw.note = d, store.Registry(d).open(), {dchat: dst}, ""
    dw.client = DlTG()
    dw.session_file = os.path.join(d, "x.session")
    dw.last_sweep = {}
    dw._busy_uids = set()
    did = await dw._drain()
    assert did == 2, f"坏的那条不该拖累好的：该下好 2 条，实际 {did}"
    assert dst.get(1)["status"] == "pending" and dst.get(1)["note"], "坏的那条要留着说明"
    assert dst.get(1)["next_try"] is None, "前 3 次失败不设等待：下轮直接接着 .part 重试"
    assert dst.get(2)["status"] == "done" and dst.get(3)["status"] == "done", "好的两条要下好"
    assert dw.client.got == [(2, 0), (3, 0)], dw.client.got     # 引用过期那条重取后从头续
    dst.close()
    print("download ok: 一条下不动只影响它自己（退避重试），后面的照常下完")

    # 网页预览那种「根本没有可下载媒体」：标 skipped，不无限重试
    class NoMedia:
        async def get_input_entity(self, chat_id):
            return "peer"

        async def get_messages(self, peer, ids):
            from telethon.tl.types import MessageMediaWebPage
            return [msg(ids[0], media=MessageMediaWebPage(webpage=types.SimpleNamespace()))]

    nchat = -1000000555555
    nst = w.store(nchat)
    nst.insert(row(nchat, 7, status="pending", size=10, file_uid=8100))
    nst.commit()
    nw = watcher.Watcher.__new__(watcher.Watcher)
    nw.root, nw.reg, nw.stores, nw.note = d, store.Registry(d).open(), {nchat: nst}, ""
    nw.client = NoMedia()
    nw.session_file = os.path.join(d, "x.session")
    nw._busy_uids = set()
    await nw._drain()
    r7 = nst.get(7)
    assert r7["status"] == "skipped" and "没有可下载的媒体" in (r7["note"] or ""), dict(r7)
    assert not r7["next_try"], "没有媒体的不该进重试队列"
    nst.close()
    print("nomedia  ok: 消息里没有可下载的媒体 → skipped 写明原因，不进无限重试")

    # --- 9.? 同 file_uid 并发防撞：相册里内容相同的两条共用一个 .part，一次只下一个 ---
    uchat = -1001101008999
    cst = w.store(uchat)
    for mid in (41, 42):
        cst.insert(row(uchat, mid, status="pending", size=10, file_uid=8999))
    cst.commit()
    cw = watcher.Watcher.__new__(watcher.Watcher)
    cw.root, cw.reg, cw.stores, cw.note = d, store.Registry(d).open(), {uchat: cst}, ""
    cw.client = DlTG()
    class SlowTG(DlTG):
        """同样标真的下的客户端，但每步都强制让出 —— 不让出的话协程根本不交错，
        防撞门就测不出来（真网线上每一步都在等 IO，中途必然交得出去）。"""

        async def get_messages(self, peer, ids):
            await asyncio.sleep(0)
            return [msg(ids[0], photo=photo(9000 + ids[0]))]

        async def iter_download(self, m, offset=0, request_size=None):
            await asyncio.sleep(0)
            yield b"z" * 10
    cw.client = SlowTG()
    cw.session_file = os.path.join(d, "x.session")
    cw.last_sweep = {}
    cw._busy_uids = set()
    did = await cw._drain()
    assert did == 1, f"同内容两条一轮只该下一条：实际 {did}"
    assert cst.get(41)["status"] == "done", dict(cst.get(41))
    assert cst.get(42)["status"] == "pending", dict(cst.get(42))
    await cw._drain()                       # 下一轮：42 单独下，按内容挂到 41 上
    assert cst.get(42)["status"] == "done", dict(cst.get(42))
    assert cst.get(42)["dup_of"] == 41, dict(cst.get(42))
    cst.close()
    print("dupuid   ok: 相册同内容（同 file_uid）不再并发往同一个 .part 里写两份")

    # --- 9.? 只看已编辑筛选 + 单独删一版历史 ---
    echat = -1001101009000
    est = store.Store(d, echat).open()
    def put(chat, rel, n=100):
        p = os.path.join(d, str(chat), rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(b"x" * n)
    est.insert(row(echat, 61, status="done", size=100, sha256="a" * 64,
                   path="files/a/" + "a" * 64 + ".jpg"))
    est.insert(row(echat, 62, status="done", size=100))   # 没编辑过的对照
    est.commit()
    est.add_revision(61)                                  # seq1：先把老文件当历史存下
    est.con.execute("UPDATE messages SET sha256=?, path=?, edited=1 WHERE msg_id=?",
                    ("b" * 64, "files/b/" + "b" * 64 + ".jpg", 61))
    est.con.execute("UPDATE revisions SET path=? WHERE seq=1",
                    ("files/a/" + "a" * 64 + ".jpg",))
    est.commit()
    put(echat, "files/a/" + "a" * 64 + ".jpg"); put(echat, "files/b/" + "b" * 64 + ".jpg")
    assert [x["msg_id"] for x in est.items(status="edited")] == [61]
    assert 62 in [x["msg_id"] for x in est.items(status="done")], "没编辑的照常在 done 里"
    freed = est.drop_revision(61, 1)                     # seq1 指的 a 文件没人引用了 → 连文件一起删
    assert freed == 100, freed
    assert not os.path.exists(os.path.join(d, str(echat), "files/a", "a" * 64 + ".jpg")), "a 文件该被删"
    assert os.path.exists(os.path.join(d, str(echat), "files/b", "b" * 64 + ".jpg")), "b 还被消息引用着，不能删"
    assert est.drop_revision(61, 99) is None, "不存在的 seq 要返回 None"
    est.close()
    print("revdel   ok: 只看已编辑筛得出；删单版历史 → 没人引用的文件连盘删、还被引用的只删索引")

    # --- 9.? 删「现在」= 回退一版，不是把整条删掉 ---
    bchat = -1001101009001
    bst = store.Store(d, bchat).open()
    put(bchat, "files/a/" + "a" * 64 + ".jpg"); put(bchat, "files/b/" + "b" * 64 + ".jpg")
    bst.insert(row(bchat, 71, status="done", size=100, sha256="b" * 64,
                   path="files/b/" + "b" * 64 + ".jpg", caption="新图"))
    bst.commit()
    bst.add_revision(71)                                  # seq1：老图（原图）当历史存下
    bst.con.execute("UPDATE messages SET sha256=?, path=?, caption=?, edited=1 WHERE msg_id=?",
                    ("a" * 64, "files/a/" + "a" * 64 + ".jpg", "老图", 71))
    bst.con.execute("UPDATE revisions SET path=?, size=100 WHERE seq=1",
                    ("files/b/" + "b" * 64 + ".jpg",))
    bst.con.execute("UPDATE messages SET link=? WHERE msg_id=?",
                    ("by-author/x/20261017_71_photo_old.jpg", 71))   # 图片时代的陈列名
    bst.commit()
    out = bst.drop_current(71)                            # 删「现在」→ 原图(b)顶上
    assert out == {"deleted": 0, "bytes": 100}, out       # 原「现在」(b) 没人要了 → 释放
    r = bst.get(71)
    assert r["path"] == "files/b/" + "b" * 64 + ".jpg", r["path"]   # 原图(历史里的 b)顶上来
    assert r["link"] is None, r["link"]                   # 陈列名必须跟着清，不能挂旧版的名字
    mm = gallery._member(dict(r), [])                     # 画廊文件地址 path 优先：
    assert mm["file"] == r["path"], mm["file"]            # link 再过期也不能给视频喂错字节
    assert r["caption"] == "新图" and r["status"] == "done", dict(r)
    assert len(bst.revisions(71)) == 0, "被顶上来那行要从历史里去掉"
    assert os.path.exists(os.path.join(d, str(bchat), "files/b", "b" * 64 + ".jpg")), "顶上来那份要还在盘上"
    assert not os.path.exists(os.path.join(d, str(bchat), "files/a", "a" * 64 + ".jpg")), "被删的那份要释放"
    out = bst.drop_current(71)                            # 历史空了：没有版可退，整条删
    assert out["deleted"] == 1, out
    assert bst.get(71) is None and not os.path.exists(
        os.path.join(d, str(bchat), "files/b", "b" * 64 + ".jpg")), "最后一版删掉后整条消失、文件释放"
    bst.close()
    print("revnow   ok: 删「现在」→ 上一版顶上来（大图还在、说明跟着回退）；删到一版不剩才整条删")

    # --- 9.11 话题聚合 / 最大文件 / 收藏 ---
    schat = -100777
    sst = store.Store(d, schat).open()
    sst.set_topic_title(111, "腿控@日常拍腿")
    sst.set_topic_title(222, "母狗鉴赏")
    sst.insert(row(schat, 1, topic_id=111, status="done", sha256="d" * 64, size=5000))
    sst.insert(row(schat, 2, topic_id=111, status="done", sha256="e" * 64, size=900))
    sst.insert(row(schat, 3, topic_id=222, status="skipped", size=700))
    sst.commit()
    tp = {t["topic_id"]: t for t in sst.topics()}
    assert tp[111]["n"] == 2 and tp[111]["title"] == "腿控@日常拍腿", tp
    assert tp[222]["n"] == 1 and tp[222]["bytes"] == 700, tp[222]
    top = sst.top(10)
    assert [r["msg_id"] for r in top] == [1, 2], [r["msg_id"] for r in top]
    assert [r["msg_id"] for r in sst.items(topic=111)] == [2, 1]
    sst.set_star(1, True)
    assert [r["msg_id"] for r in sst.items(status="star")] == [1]
    assert sst.items(status="deleted") == []
    sst.delete([3])                                       # 话题 222 全删光
    sst.commit()
    tp = {t["topic_id"]: t for t in sst.topics()}
    assert tp[222]["n"] == 0 and tp[111]["n"] == 2, tp    # 删空的话题也要留在列表里
    sst.close()
    print("topics   ok: 话题聚合带标题和条数、最大文件只算下好的、收藏和话题都能筛")

    # --- 9.12 局域网密码：只存哈希、没设密码不放行 ---
    salt, h = server.hash_pw("hunter2")
    assert server.check_pw("hunter2", salt, h) and not server.check_pw("hunter3", salt, h)
    assert "hunter2" not in h and len(h) == 64
    assert not server.check_pw("", "", ""), "没设密码时不能放行"
    print("lanpw    ok: 密码只存 PBKDF2 哈希、验证正确、没设密码时不放行")

    # --- 9.14 大文件并行下载的纯逻辑（不连网，_lane_plan/侧账/_sha_file）---
    lchat = -1001101009002
    lst = store.Store(d, lchat).open()
    total = 10 * watcher.MiB + 7
    plan = watcher._lane_plan(total, watcher.DOWNLOAD_FILE_LANES)
    assert sum(n for _, n in plan) == -(-total // watcher.LANE_CHUNK), "块数要覆盖整个文件"
    assert plan[0][0] == 0, "第一段必须从 0 开始"
    assert [s for s, _ in plan] == sorted(s for s, _ in plan), "段起点要递增"
    for s, n in plan:                          # 每段起点对齐块边界（官方 4KB 倍数的超集）
        assert s % watcher.LANE_CHUNK == 0
    p1 = watcher._lane_plan(3 * watcher.LANE_CHUNK + 11, 1)
    assert p1 == [(0, 4)], p1                  # 单 lane = 一整串
    # 块账：全 lane 记满才算完整，缺一块就不完整（不许误落盘）
    plan2 = watcher._lane_plan(5 * watcher.LANE_CHUNK, 2)
    prog = {"chunk": watcher.LANE_CHUNK, "lanes": 2, "done": [1, 0]}
    assert not all(prog["done"][i] >= n for i, (_s, n) in enumerate(plan2))
    prog["done"] = [2, 3]                      # ceil 均分：_lane_plan(25KB×…)/2 → lane0 分到 2 块、lane1 3 块
    assert all(prog["done"][i] >= n for i, (_s, n) in enumerate(plan2))
    # sha 助手与 hashlib 一致（并行路径下完了就是用它按序读回）
    payload = os.urandom(3 * watcher.MiB + 5)
    sp = os.path.join(d, "sha_probe.bin")
    with open(sp, "wb") as fh:
        fh.write(payload)
    assert watcher._sha_file(sp) == hashlib.sha256(payload).hexdigest()
    os.remove(sp)
    lst.close()
    print("lanes    ok: 块账切分对齐、尾块、全记满才算完整；_sha_file 与 hashlib 一致")

    # --- 9.15 手动重试「下载失败」：行和缩略图不动，只把状态放回队列 ---
    rchat = -1001101009003
    rst = store.Store(d, rchat).open()
    assert "error" in rst.retry_one(1), "库里没有这条要报错"
    rst.insert(row(rchat, 1, status="done", path="files/aa/x.jpg", thumb="thumbs/rt.jpg"))
    assert "error" in rst.retry_one(1), "done 行不该能重试"
    rst.set_status(1, "failed", "下载超时（连接卡住了），等会儿再试")
    assert rst.retry_one(1) == {}
    r = rst.get(1)
    assert r["status"] == "pending" and not r["tries"] and not r["note"], dict(r)
    assert r["thumb"] == "thumbs/rt.jpg", "缩略图不能被重试动掉"
    rst.close()
    print("retryone ok: 失败行放回队列、行/缩略图不动；done 行与不存在的行给错误")

    # --- 9.13 删除和重复行的两个坑（都是真实会踩到的）---
    dchat = -100888
    dst = store.Store(d, dchat).open()
    blob = dst.abs(store.blob_rel("f" * 64, ".jpg"))
    os.makedirs(os.path.dirname(blob), exist_ok=True)
    open(blob, "wb").write(b"x" * 10)
    thumb = dst.abs("thumbs/1.jpg")
    os.makedirs(os.path.dirname(thumb), exist_ok=True)
    open(thumb, "wb").write(b"t")
    # 1 是原件，2 是「同一个文件重发」的重复行：thumb 存的就是原件那张图
    dst.insert(row(dchat, 1, status="done", sha256="f" * 64, path=store.blob_rel("f" * 64, ".jpg"),
                   thumb="thumbs/1.jpg", size=10))
    dst.insert(row(dchat, 2, status="dup", sha256="f" * 64,
                   path=store.blob_rel("f" * 64, ".jpg"), thumb="thumbs/1.jpg", dup_of=1))
    dst.commit()
    dst.delete([2])
    assert os.path.exists(thumb), "删重复行不能顺手把原件的缩略图删掉"
    assert os.path.exists(blob), "原件还在，内容文件当然要留着"

    # 3 是原件、4 挂着它：删掉原件之后 4 必须变成能独立显示的卡片
    dst.insert(row(dchat, 3, status="done", sha256="g" * 64, path=store.blob_rel("g" * 64, ".jpg"),
                   thumb="thumbs/3.jpg", size=10))
    dst.insert(row(dchat, 4, status="dup", dup_of=3))
    dst.insert(row(dchat, 5, status="dup", dup_of=3))
    dst.commit()
    dst.delete([3])
    r4, r5 = dst.get(4), dst.get(5)
    assert r4["dup_of"] is None and r4["status"] == "pending", dict(r4)
    assert r5["dup_of"] == 4, "剩下的重复行要改指新的原件"
    # 原件已经下好过（有 path）的那种，接手的直接算下好
    dst.insert(row(dchat, 6, status="done", sha256="h" * 64, path=store.blob_rel("h" * 64, ".jpg"),
                   thumb="thumbs/6.jpg", size=10))
    dst.insert(row(dchat, 7, status="dup", path=store.blob_rel("h" * 64, ".jpg"), dup_of=6))
    dst.commit()
    dst.delete([6])
    r7 = dst.get(7)
    assert r7["dup_of"] is None and r7["status"] == "done", dict(r7)
    dst.close()
    print("dupdel   ok: 删重复行不误删原件缩略图；删原件后重复行接位（有文件算 done，没文件去重下）")

    # --- 9.14 下载中途被删：不能把文件落到盘上变成孤儿 ---
    ochat = -1000000333333
    ost = w.store(ochat)
    ost.insert(row(ochat, 60, status="pending", file_uid=77000, size=100))
    ost.commit()
    part = ost.abs("tmp/77000.part")
    os.makedirs(os.path.dirname(part), exist_ok=True)
    real_grab = watcher.grab_full

    async def fake_grab(client, st, msg, r):
        return "a" * 64, None

    watcher.grab_full = fake_grab
    try:
        ost.delete([60])                       # 下载还没回来，用户就把这条删了
        assert ost.get(60) is None and not os.path.exists(part), "删掉了就该连 .part 一起清"
        open(part, "wb").write(b"z" * 100)     # 下载线程还握着它，又写出来了
        out = await watcher.finish(None, ost, None,
                                   dict(row(ochat, 60, file_uid=77000, mime="image/jpeg")))
        assert out == (None, None), out
        blob = ost.abs(store.blob_rel("a" * 64, ".jpg"))
        assert not os.path.exists(blob), "行都没了，不该再落一份没人引用的文件（used_bytes 数不到它）"
        assert not os.path.exists(part), ".part 也该清掉"
    finally:
        watcher.grab_full = real_grab
    ost.close()
    print("orphan   ok: 下载中途被删的行不再落文件（否则就是磁盘上限数不到的孤儿）")

    # --- 9.15 凭证加密：DPAPI 打底 + 可选口令（secure.py / BlobSession）---
    sd = os.path.join(d, "sec")
    os.makedirs(sd, exist_ok=True)
    salt = secure.new_salt()
    ent = secure.scrypt_entropy("hunter2", salt)
    raw = b"\x00auth-key\xff" * 64
    sealed = secure.seal(raw, ent)
    assert secure.unseal(sealed, ent) == raw
    assert raw[:12] not in sealed, "密文里不该出现明文"
    assert secure.seal(raw) != sealed, "带熵和不带熵必须是两种密文"
    assert secure.scrypt_entropy("hunter2", salt) == ent, "同一个盐必须派生出同一把钥匙"
    assert secure.scrypt_entropy("hunter2", secure.new_salt()) != ent, "换了盐就得换钥匙"
    for bad in (None, secure.scrypt_entropy("hunter3", salt)):
        try:
            secure.unseal(sealed, bad)
            assert False, "错熵/没熵不该解得开"
        except OSError:
            pass
    print("secure   ok: 密封往返、密文不含明文、错熵解不开、scrypt 跟着盐走")

    sreg = store.Registry(sd).open()
    sealer = secure.Sealer("hunter2", salt)
    bs = watcher.BlobSession(sreg, sealer)
    assert bs.auth_key is None and not sreg.setting("session_blob", ""), "没登录不该写空库"
    key = bytes(range(256))
    bs.auth_key = AuthKey(key)
    bs.save()
    blob = sreg.setting("session_blob", "")
    assert blob and key[:32] not in secure.b64d(blob), "auth_key 明文绝不能进密文"
    bs2 = watcher.BlobSession(sreg, sealer)
    assert bs2.auth_key is not None and bs2.auth_key.key == key, "重启后要把整库读回来"
    try:
        watcher.BlobSession(sreg, secure.Sealer())
        assert False, "没有口令就不该解得开"
    except OSError:
        pass
    print("blobses  ok: session 只在内存里跑、落盘是密文；没登录不写空库、没口令读不回来")

    # 设了口令：重启必须锁着、不建客户端、错口令解不开、连错 5 次要拖一下
    ld = os.path.join(d, "sec2")
    os.makedirs(ld, exist_ok=True)
    w8 = bare_watcher(ld)
    w8.cfg.save(12345678, "a" * 32)
    await w8.boot()
    assert w8.state == "login" and not w8.locked, "没口令时行为要跟以前一样"
    assert (await w8.set_passcode("hunter2"))["ok"]
    w9 = bare_watcher(ld)
    await w9.boot()
    assert w9.state == "locked" and w9.locked and w9.client is None, "设了口令就必须锁着"
    assert (await w9.unlock("hunter3"))["ok"] is False and w9.state == "locked"
    for _ in range(4):
        await w9.unlock("hunter3")
    slow = await w9.unlock("hunter3")
    assert not slow["ok"] and "秒后再试" in slow["error"], "连错 5 次要拖一下"
    w9.pw_until = 0                        # 别让测试真等 30 秒
    good = await w9.unlock("hunter2")
    assert good["ok"] and not w9.locked and w9.state != "locked", "对口令必须解得开"
    assert w9.cfg.api_hash == "a" * 32, "解锁后 api_hash 要能用（重封过一遍）"
    assert (await w9.clear_passcode())["ok"] and not w9.reg.setting("pw_salt", "")
    w10 = bare_watcher(ld)
    await w10.boot()
    assert w10.state == "login" and not w10.locked, "取消口令后又该自动解开"
    print("lock     ok: 设口令后锁着不连、错口令拒绝并限速、对口令解开、取消后自动解开")

    lreg = w9.reg
    rows = lreg.con.execute("SELECT key, value FROM settings").fetchall()
    assert not any(v == "a" * 32 for _k, v in rows), "settings 里不能有明文 api_hash"
    assert not any(v == "hunter2" for _k, v in rows), "settings 里不能有明文口令"
    assert lreg.setting("api_hash_enc", "") and not lreg.setting("api_hash", "")
    lreg.set_setting("api_hash", "b" * 32)                 # 假装是老版本留下的明文
    store.Config(ld, secure.Sealer()).migrate_plaintext(secure.Sealer())
    assert not lreg.setting("api_hash", ""), "老版本的明文 api_hash 必须就地清掉"
    assert store.Config(ld).api_hash == "b" * 32, "加密后还要读得回来"
    print("atrest   ok: 落盘的只有密文（api_hash/口令都搜不到明文），老明文会自动加密并删掉")

    w8.reg.close()
    w9.reg.close()
    w10.reg.close()
    sreg.close()

    # --- 左栏列表：开着监听的置顶，其余（停了的、没配的）按标题混排 ---
    w0 = fake_watcher(d)
    w0.reg.set_watch(-100771, title="乙群", enabled=True)
    w0.reg.set_watch(-100772, title="丙群", enabled=False)
    merged = w0._merge_watches([{"chat_id": -100773, "title": "戊频道", "kind": "频道"}])
    ids = [c["chat_id"] for c in merged]
    assert ids.index(-100771) < ids.index(-100772) < ids.index(-100773), ids
    w0.reg.close()
    print("chats 排序 ok: 开着的监听置顶，停了的/没配的按标题混排")

    # --- 离线导出：真渲染一份，瀑布流/筛选/灯箱的骨架都在 ---
    out = os.path.join(d, "_gallery_smoke.html")
    st.author_dir(555, "老王")
    st.insert(row(CHAT, 61, status="done", kind="photo",
                  path="files/x.jpg", thumb="thumbs/x.jpg", topic_id=7))
    st.insert(row(CHAT, 62, grouped_id=55, status="done",
                  kind="video", path="files/y.mp4", author_id=666))
    st.commit()
    nrows = gallery.render(out, st.gallery_rows(), "测试群", CHAT)
    assert nrows >= 2, nrows
    txt = open(out, encoding="utf-8").read()
    for must in ('id="s"', 'id="au"', 'id="tp"', "lbox", "grid-auto-rows",
                 'class="mb"', "data-au"):
        assert must in txt, must
    print("gallery  ok: 离线页真渲染，瀑布流/状态/作者/话题筛选/灯箱骨架齐全")

    st.close()
    st2.close()
    reg.close()
    w.reg.close()
    print("\nALL OK")


asyncio.run(main())
