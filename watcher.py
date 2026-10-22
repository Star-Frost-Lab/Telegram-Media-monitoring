"""监听器：Telethon 客户端 + 新消息事件 + 下载队列 + 兜底补漏。

主机制是**推送**：events.NewMessage 收到 update 就处理，不是轮询。

另外留了一个低频兜底扫描，理由是实测出来的：Telethon 确实会处理频道更新的 pts 缺口
（_updates/messagebox.py:745 按存的 pts 要 GetChannelDifference），但它在拿不到该频道
access hash 时会直接放弃并丢掉 pts 记录（同文件 733-739）—— 那种情况会静默丢消息。
兜底扫描从游标往回翻，INSERT OR IGNORE 天然幂等，等于是免费的保险。

所以：**监听器不推进游标**，游标只由兜底扫描推进。游标一旦被监听器推到最新，
兜底就没东西可查了，缺口会被永久跳过。
"""

import asyncio
import base64
import hashlib
import io
import json
import os
import sqlite3
import time

import qrcode
from telethon import TelegramClient, events
from telethon import utils as tgutils
from telethon.sessions import SQLiteSession
from telethon.tl import functions
from telethon.sessions import SQLiteSession
from telethon.errors import (FileReferenceExpiredError, FloodWaitError,
                             PasswordHashInvalidError, SessionPasswordNeededError)
from telethon.tl.functions.messages import (GetForumTopicsByIDRequest,
                                            GetHistoryRequest)
from telethon.tl.types import (Channel, Chat, ChatPhotoEmpty,
                               DocumentAttributeSticker,
                               DocumentAttributeVideo, InputDocumentFileLocation,
                               InputPhotoFileLocation, MessageMediaWebPage,
                               PhotoSize, PhotoSizeProgressive)

import gallery
import secure
import store as store_mod
import tg

MiB = 1 << 20
SWEEP_SECONDS = 600      # 兜底扫描间隔
FROZEN_S = 5             # 下载循环一觉睡过头这么多秒，就认定本进程被系统挂起过


def frozen_seconds(t0, nap, t1):
    """这一觉比要的睡多了多久 —— 多出来的就是进程没被调度的时间。

    系统进待机（这台机器只有 Modern Standby）时 Windows 会把桌面程序整个挂起，
    服务在那段时间里根本不跑，手机自然连不上。这条只用来把「有没有被挂起」写进日志。
    """
    return max(0.0, (t1 - t0) - nap)
ALBUM_SETTLE = 3.0       # 相册成员之间最多隔这么久，等齐了一起下
# 同时下几条。参考项目（Telegram-bot-downloader bot.py:69）注释写明 >2 会触发 Telegram
# 的连接数限制，所以他们也是 2。
DOWNLOAD_CONCURRENCY = 8
# 大文件并行下载。一条连接一次只等一个 512KB 请求回来，速度被「块大小 ÷ 往返延迟」卡死；
# 调研结论（core.telegram.org/api/files）：单请求上限就是 1MB/precise，Telegram 不给更大的块，
# 唯一的路是同一文件开多条连接并发拉 —— 超过 PAR_MIN 的文件拆 LANES 条，进度记在
# .part.prog 侧账里，断点续传按块账走。参考 painor 的 fast-telethon（gist 7e74de80）。
DOWNLOAD_FILE_PAR_MIN = 8 * MiB   # 小于它串行足够；也保证 >8MB 的都是 document（有 dc_id）
DOWNLOAD_FILE_LANES = 4           # 每个大文件几条连接，保守起步，可调
DOWNLOAD_FILE_PAR_FILES = 2       # 同时处于并行模式的文件数上限：每 lane 一次授权握手，太重
LANE_CHUNK = 512 * 1024           # 并行路径的单请求块（= Telethon MAX_CHUNK_SIZE）
DOWNLOAD_MAX_TRIES = 8            # 同一条消息连续失败这么多次就进「下载失败」（带一键重试）
_PAR_SEM = None                   # 并行模式的文件数闸门（首次用时建，asyncio 惰性）；
                                  # FLOOD_WAIT 是服务端限流不算失败，照常退避等
# 这条消息里没有可下载的东西（被撤回、或被改成纯文字/链接了）。永久性的，别再重试。
MEDIA_GONE = "这条消息里没有可下载的媒体（可能已被撤回，或改成了纯文字/链接）"

# session 被服务器注销后抛的这些：本地文件还说「已授权」，只有真连一次才发现
DEAD_SESSION = ("AuthKeyUnregisteredError", "AuthKeyDuplicatedError", "SessionRevokedError",
                "SessionExpiredError", "UserDeactivatedError", "UserDeactivatedBanError")


def hint(exc):
    """给网页看的错误串。超时补一句时间提示：本机时钟比服务器慢 30 秒以上时，服务器
    发来的一切都会被 Telethon 丢掉（network/mtprotostate.py:20 MSG_TOO_NEW_DELTA=30），
    表现就是请求一直超时。"""
    s = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, TimeoutError):
        s += "（若反复超时：检查系统时间有没有跟网络同步，差 30 秒以上会被服务器丢弃）"
    return s


# --- 收什么不收什么 ---

def allowed(w, sender_id):
    """这群开了作者白名单吗，这条消息的作者在不在里面。

    w["authors"] 是逗号分隔的用户 ID，空 = 不限。系统消息没有 sender_id，
    在任何名单里都算不在。
    """
    ids = (w["authors"] or "") if w else ""
    if not ids.strip():
        return True
    return sender_id is not None and str(sender_id) in \
        {s.strip() for s in ids.split(",") if s.strip()}


def biggest_photo(photo):
    """照片里最大的那个尺寸，用来估大小和长宽。"""
    w = h = size = 0
    for s in photo.sizes or []:
        if isinstance(s, PhotoSizeProgressive):
            n = max(s.sizes) if s.sizes else 0
        else:
            n = getattr(s, "size", 0) or 0
        if n and s.w * s.h >= w * h:
            w, h, size = s.w, s.h, n
    return w, h, size


def full_photo_type(photo):
    """占位最大的普通（非 stripped）尺寸的类型字符：下全图要用它当 thumb_size。"""
    best, bw, btype = 0, 0, ""
    for s in photo.sizes or []:
        if isinstance(s, (PhotoSize, PhotoSizeProgressive)):
            n = max(s.sizes) if isinstance(s, PhotoSizeProgressive) and s.sizes \
                else (getattr(s, "size", 0) or 0)
            if n and s.w * s.h >= bw:
                best, bw, btype = n, s.w * s.h, s.type
    return btype


def media_ref(ty, obj, ts=""):
    """入库时把「下载门票」存成 JSON：id 之外的 access_hash + file_reference。

    下载根本不需要消息，只要这三个字段（inputPhotoFileLocation.md /
    inputDocumentFileLocation.md）：消息只是容器，撤回删的是容器，blob 还在服务器上。
    file_reference 是服务端会轮换的一次性票据 —— 只能当「门票」存，绝不能当去重键
    （去重两层是 file_uid=同一文件对象、sha256=同样字节，跟它无关）。"""
    if obj is None:
        return ""
    return json.dumps(
        {"ty": ty, "ah": obj.access_hash,
         "fr": base64.b64encode(obj.file_reference or b"").decode(), "ts": ts},
        separators=(",", ":"))


def classify(m):
    """(媒体信息, 不要的理由)。

    贴纸认 DocumentAttributeSticker，不认容器格式：动态 emoji 是 video/webm、
    静态贴纸是 image/webp，光看 mime 会把它们全收进来。
    """
    photo = getattr(m, "photo", None)
    if photo is not None:
        w, h, size = biggest_photo(photo)
        return {"kind": "photo", "file_uid": photo.id, "size": size,
                "mime": "image/jpeg", "width": w, "height": h, "duration": None,
                "ref": media_ref("p", photo, full_photo_type(photo))}, None

    doc = getattr(m, "document", None)
    if doc is None:
        return None, "no-media"
    attrs = list(getattr(doc, "attributes", None) or [])
    if any(isinstance(a, DocumentAttributeSticker) for a in attrs):
        return None, "sticker"
    mime = (getattr(doc, "mime_type", "") or "").lower()
    if not (mime.startswith("image/") or mime.startswith("video/")):
        return None, mime or "unknown"
    w = h = 0
    dur = None
    for a in attrs:
        if isinstance(a, DocumentAttributeVideo):
            w, h, dur = a.w, a.h, a.duration
    return {"kind": "photo" if mime.startswith("image/") else "video",
            "file_uid": doc.id, "size": doc.size, "mime": mime,
            "width": w, "height": h, "duration": dur,
            "ref": media_ref("d", doc)}, None


# --- 往回翻页（兜底扫描用）---

async def fetch_page(client, peer, before_id, limit, tries=4):
    """用 getHistory 的原生方向（从新往旧）+ offset_id 往回翻。

    不碰 min_id/max_id —— Telethon 自己的注释说 Telegram 对这两个的处理很别扭
    （client/messages.py:33-37），游标这种东西不能靠猜。
    """
    for i in range(tries):
        try:
            res = await client(GetHistoryRequest(
                peer=peer, offset_id=before_id, offset_date=None, add_offset=0,
                limit=limit, max_id=0, min_id=0, hash=0))
            return list(res.messages)
        except FloodWaitError as exc:
            if i == tries - 1:
                raise
            print(f"  FLOOD_WAIT {exc.seconds}s，等着", flush=True)
            await asyncio.sleep(exc.seconds + 1)


async def fetch_pages(fetch, cursor, page=100, max_pages=200):
    """从最新往回翻到游标为止，返回按 id 升序的消息。"""
    out, before, pages = [], 0, 0
    while pages < max_pages:
        msgs = await fetch(before, page)
        if not msgs:
            break
        pages += 1
        fresh = [m for m in msgs if m.id > cursor]
        out.extend(fresh)
        if len(fresh) < len(msgs):            # 这页里有 <= 游标的，翻到头了
            break
        before = msgs[-1].id
        if len(msgs) < page:                  # 历史到底了
            break
    out.sort(key=lambda m: m.id)
    return out


async def resolve_topics(client, peer, st, ids):
    """把见到的话题号换成标题，界面上才好认（这群有 2432 个话题）。"""
    ids = [i for i in ids if i]
    for i in range(0, len(ids), 100):
        try:
            res = await client(GetForumTopicsByIDRequest(peer=peer, topics=ids[i:i + 100]))
        except Exception as exc:
            print(f"  话题标题取不到（{type(exc).__name__}），不影响归档", flush=True)
            return
        for t in getattr(res, "topics", None) or []:
            st.set_topic_title(getattr(t, "id", None), getattr(t, "title", None))


# --- 下载 ---

def make_link(st, row, blob_abs, sha, mime):
    """在作者目录里做个硬链接，按作者翻文件用，不额外占空间。

    硬链接失败（跨盘之类）不影响归档，界面会退回用真身路径显示。
    """
    rel = store_mod.link_rel(st.author_dir(row["author_id"], None), row["date"],
                             row["msg_id"], row["kind"], sha, store_mod.ext_of(mime))
    dst = st.abs(rel)
    try:
        if not os.path.exists(dst):
            os.link(blob_abs, dst)
        return rel
    except OSError:
        return None


async def grab_thumb(client, st, msg, row):
    """缩略图几 KB，入库时就抓，条目一出现就能看到图。"""
    rel = f"thumbs/{row['file_uid']}.jpg"
    if os.path.exists(st.abs(rel)):
        return rel
    try:
        data = await client.download_media(msg, file=bytes, thumb=-1)
    except FloodWaitError as exc:
        await asyncio.sleep(exc.seconds + 1)
        return None
    except Exception:
        return None
    if not data:
        return ""                              # 没缩略图，空串表示试过了别再试
    with open(st.abs(rel), "wb") as fh:
        fh.write(data)
    return rel


async def refetch(client, msg):
    """重新取一遍这条消息，用来刷新过期的 file_reference。

    受保护群里的消息放久了 file_reference 会过期，之后每次下载都抛 FileReferenceExpiredError，
    不重新取就永远下不动（参考项目 downloader.py:1206-1225 也是这么做的）。

    取不回来（网络问题）返回 None；取回来了就把那条消息交回去 —— 里面还有没有媒体由调用方判。
    """
    try:
        peer = await client.get_input_entity(msg.chat_id)
        got = await client.get_messages(peer, ids=msg.id)      # 单个 id：拿回来就是一条
    except Exception:
        return None
    return (got[0] if got else None) if isinstance(got, list) else got


_REFETCH = "__refetch__"   # _download_lanes 的内部信号：引用过期，grab_full 去重取消息


def _lane_plan(total, lanes, chunk=LANE_CHUNK):
    """把 total 个字节切成 lanes 条连续区段：[(区段起点, 该段块数), ...]。

    起点都对齐到 chunk（官方要求 getFile 的 offset/limit 是 4KB 倍数，chunk 本身就是）。
    各段块数按 ceil 均分，最后一段承担文件的尾巴（尾块可能不满 chunk）。
    """
    chunks = -(-total // chunk)
    return [((chunks * i // lanes) * chunk,
             chunks * (i + 1) // lanes - chunks * i // lanes)
            for i in range(lanes)]


# 下载期间的整文件 SHA-256 挪到线程：loop 是唯一通道，同步哈希会把网页 API 和局域网图片全冻住
def _sha_file(path):
    """按序整读一个文件算 sha256。并行路径的块是乱序落盘的，只能下完再算。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(MiB), b""):
            h.update(block)
    return h.hexdigest()


async def _download_lanes(client, st, msg, row, part, prog_path):
    """大文件并行下载：LANES 条连接各自按自己的区段顺序拉。

    .part 预分配成 total 大小（稀疏，NTFS 上写到哪才占哪），块写到任意绝对位置；
    进度记在 prog_path（JSON，块账）。外层 grab_full 也要能走串行 ——
    返回 (sha256, 错误说明)，错误说明为 _REFETCH 时由调用方重取消息后整段重来。
    """
    total = row["size"]
    lanes = DOWNLOAD_FILE_LANES
    chunk = LANE_CHUNK
    prog = None
    if os.path.exists(prog_path):
        try:
            with open(prog_path, encoding="utf-8") as fh:
                prog = json.load(fh)
        except (OSError, ValueError):
            prog = None
        # 块参数对不上（改过配置/代码）就整份作废重来，宁慢勿错
        if prog is not None and (prog.get("chunk") != chunk or prog.get("lanes") != lanes):
            prog = None
    if prog is None:
        with open(part, "wb"):
            pass                               # 稀疏预分配：先不占盘，写到哪才占哪
        os.truncate(part, total)
        prog = {"chunk": chunk, "lanes": lanes, "done": [0] * lanes}
    plan = _lane_plan(total, lanes, chunk)
    print(f"  [{row['msg_id']}] 大文件并行下载 {total / MiB:.0f} MB × {lanes} 条连接", flush=True)

    def save_prog():
        tmp = prog_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(prog, fh)
        os.replace(tmp, prog_path)

    dc_id, location = tgutils.get_input_location(msg.media)
    senders = []
    tasks = []
    status = None
    try:
        for _ in range(lanes):                 # 一 lane 一条真连接（_borrow 那个是按 dc 缓存共享的，用不了）
            senders.append(await client._create_exported_sender(dc_id))

        async def _lane(i, sender):
            start, n = plan[i]
            with open(part, "r+b") as fh:
                off = start + prog["done"][i] * chunk
                for k in range(prog["done"][i], n):
                    data = (await sender.send(functions.upload.GetFileRequest(
                        location, offset=off, limit=chunk))).bytes
                    if not data:
                        return                 # 服务器断流：下轮从块账接着来
                    fh.seek(off)               # 先写后记账：断在中间也只是多一坨字节，块账说没下就是没下
                    fh.write(data)
                    prog["done"][i] = k + 1
                    save_prog()
                    if len(data) < chunk:      # 文件尾（只该出现在最后一段的尾块）
                        return
                    off += len(data)

        tasks = [asyncio.ensure_future(_lane(i, s)) for i, s in enumerate(senders)]
        await asyncio.gather(*tasks)
    except FloodWaitError as exc:
        await asyncio.sleep(exc.seconds + 1)   # 跟串行路径一致：真等够了再放手
        status = f"FLOOD_WAIT {exc.seconds}s，.part 留着下轮接着下"
    except FileReferenceExpiredError:
        status = _REFETCH                      # 剩下的 lane 一起收掉，重取消息后重来
    except Exception as exc:
        status = f"{type(exc).__name__}: {exc}"
    finally:
        for t in tasks:                        # gather 不替我们取消兄弟协程，得自己收
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for s in senders:
            try:
                await s.disconnect()
            except Exception:
                pass
        save_prog()

    if status:
        return None, status
    if not all(prog["done"][i] >= n for i, (_s, n) in enumerate(plan)):
        return None, f"并行下载没下满（块账 {sum(prog['done'])}/{sum(n for _s, n in plan)}），下轮接着来"
    os.remove(prog_path)
    return await asyncio.to_thread(_sha_file, part), None


async def grab_full(client, st, msg, row):
    """下真身。小文件走串行（原逻辑、断点续传按 1MiB 对齐），大文件走 _download_lanes。

    返回 (sha256, 错误)。一条下不动的消息**绝不能**把整个队列带走 —— 之前这里只 catch 了
    FloodWaitError，别的异常直接抛到下载线程外，整轮作废、后面的消息永远轮不到（日志里
    那个 598 次的 TypeError 就是这么来的）。所有异常都收成 (None, 错误说明)。
    """
    media = getattr(msg, "media", None)
    if media is None or isinstance(media, MessageMediaWebPage):
        return None, MEDIA_GONE
    part = st.abs(f"tmp/{row['file_uid']}.part")
    total = row["size"] or 0
    prog_path = part + ".prog"
    # 大文件才值得开多条连接；.part 无侧账 = 旧串行格式留下的半成品，继续用串行下完它。
    # 媒体上拿不到 dc_id（意外形态）也不硬上并行 —— 否则下一轮还会在同一个地方绊倒。
    # 文件已经在账号自己连着的 DC：导出授权这条路本身就不允许（DcIdInvalidError），
    # Telethon 原生对同 DC 就是直接用主连接 —— 我们同 DC 回退串行，别去送授权请求。
    use_par = False
    if total >= DOWNLOAD_FILE_PAR_MIN and (not os.path.exists(part) or os.path.exists(prog_path)):
        try:
            dc_id, _loc = tgutils.get_input_location(media)
            use_par = dc_id is not None and dc_id != client.session.dc_id
        except Exception:
            pass                               # malformed media：走串行让它报出真实错误
    if use_par:
        global _PAR_SEM
        if _PAR_SEM is None:                   # 同时并行的大文件数上限：每 lane 都要一次授权握手
            _PAR_SEM = asyncio.Semaphore(DOWNLOAD_FILE_PAR_FILES)
        async with _PAR_SEM:
            for attempt in range(2):           # 引用过期时重取一次重来（块账接着用）
                sha, err = await _download_lanes(client, st, msg, row, part, prog_path)
                if err != _REFETCH:
                    return sha, err
                if attempt:
                    return None, "文件引用又过期了，.part 留着下轮接着下"
                fresh = await refetch(client, msg)
                if fresh is None:              # 网络问题取不回来：是暂时的，下轮再试
                    return None, "文件引用过期，消息没取回来，.part 留着下轮接着下"
                if getattr(fresh, "media", None) is None or \
                        isinstance(fresh.media, MessageMediaWebPage):
                    return None, MEDIA_GONE
                msg = fresh
                continue
        return None, "并行下载循环走完了还没有结果（不该出现）"
    return await _grab_full_serial(client, st, msg, row, part, total)


async def _grab_full_serial(client, st, msg, row, part, total):
    """串行下载（原 grab_full 主体）。"""
    h = hashlib.sha256()
    got = 0
    for attempt in range(2):                       # 引用过期时重取一次再续，第二次还不行就下轮再说
        have = os.path.getsize(part) if os.path.exists(part) else 0
        start = store_mod.resume_offset(have, total)
        if start != have:                          # 砍到 1 MiB 对齐点
            with open(part, "r+b") as fh:
                fh.truncate(start)
        # 已经下过的部分要重新喂进哈希：整文件重读，挪线程别冻 loop（大 .part 一读好几秒）
        def _rehash_prefix():
            hh = hashlib.sha256()
            with open(part, "rb") as fh:
                for block in iter(lambda: fh.read(MiB), b""):
                    hh.update(block)
            return hh
        h = await asyncio.to_thread(_rehash_prefix) if start else hashlib.sha256()
        got = start
        try:
            with open(part, "ab") as fh:
                # request_size 必须 ≤ Telethon 的 MAX_CHUNK_SIZE=512KB（downloads.py:25）。
                # 传 1MiB 会被静默砍半，且 chunk_size≠request_size 时落入 _GenericDownloadIter
                # 慢路径（downloads.py:749 的条件）—— 512KB 恰好登上直接快路径。
                # 块上限是协议级的：官方 upload.getFile 要求 limit 整除 1048576，再大不可能。
                async for chunk in client.iter_download(msg, offset=start, request_size=512 * 1024):
                    fh.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds + 1)
            return None, f"FLOOD_WAIT {exc.seconds}s，.part 留着下轮接着下"
        except FileReferenceExpiredError:
            if attempt:
                return None, "文件引用又过期了，.part 留着下轮接着下"
            fresh = await refetch(client, msg)
            if fresh is None:                       # 网络问题取不回来：是暂时的，下轮再试
                return None, "文件引用过期，消息没取回来，.part 留着下轮接着下"
            if getattr(fresh, "media", None) is None or \
                    isinstance(fresh.media, MessageMediaWebPage):
                return None, MEDIA_GONE
            msg = fresh
            continue
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"
        break
    if total and got != total:
        return None, f"只下到 {got}/{total} 字节，.part 留着下轮接着下"
    # 落盘前最后一道闸：.part 必须正好是总大小。早期这里如果两条同内容消息
    # 一左一右往同一个 .part 里 append，会下出 [完整][又一份] 的 2 倍大文件。
    part_size = os.path.getsize(part)
    if total and part_size != total:
        return None, f".part 大小 {part_size} ≠ 总大小 {total}，可疑，等下轮重试"
    return h.hexdigest(), None


def is_new_edit(row, m):
    """这条消息的 edit_date 算不算「我们归档之后才发生的编辑」。

    edit_seen 是我们已经处理过的那次编辑，added 是入库时间，两者取大。归档之前就带着
    edit_date 的消息（作者早改过、相册成员被 Telegram 补过时间戳）不算新编辑 ——
    否则每轮兜底扫描都会当成一次编辑，卡片上白挂一个「已编辑」。
    """
    if row is None or not m.edit_date:
        return False
    return int(m.edit_date.timestamp()) > max(row["edit_seen"] or 0, row["added"] or 0)


def _drop_part(st, file_uid):
    """tmp 里这一份半成品连侧账一起清（msg 被删 / 内容与旧文件相同）。"""
    for suffix in ("", ".prog"):
        try:
            os.remove(st.abs(f"tmp/{file_uid}.part" + suffix))
        except OSError:
            pass


# 撤回后凭票直下被服务端拒掉的场景：引用过期且来源（消息）已撤 → 刷新不了，真的下不来
REF_GONE = "文件引用过期且消息已被撤回，刷新不了了"


async def grab_by_ref(client, st, row):
    """消息（容器）没了也能下：按入库时存的 id/access_hash/file_reference 拼 InputFileLocation
    直接拉（upload.getFile 的全部输入就这三样，见 inputPhotoFileLocation.md）。"""
    try:
        ref = json.loads(row["media_ref"])
        fr = base64.b64decode(ref["fr"])
        if ref.get("ty") == "p":
            loc = InputPhotoFileLocation(id=row["file_uid"], access_hash=ref["ah"],
                                         file_reference=fr, thumb_size=ref.get("ts") or "")
        else:
            loc = InputDocumentFileLocation(id=row["file_uid"], access_hash=ref["ah"],
                                            file_reference=fr, thumb_size="")
    except (KeyError, TypeError, ValueError):
        return None, "入库时存的位置坏了，解析不出来"
    part = st.abs(f"tmp/{row['file_uid']}.part")
    try:
        # 低层 download_file 直接吃 InputFileLocation，内部管 CDN 重定向；file=路径流式落盘
        await client.download_file(loc, file=part, file_size=row["size"] or None)
    except FileReferenceExpiredError:
        return None, REF_GONE
    return await asyncio.to_thread(_sha_file, part), None


async def finish(client, st, msg, row):
    """下完了：按内容落盘、按作者建链接、把等它的重复行补全。"""
    sha, err = await grab_full(client, st, msg, row)
    if err:
        return None, err
    part = st.abs(f"tmp/{row['file_uid']}.part")
    return _place(st, row, part, sha)


async def finish_raw(client, st, row):
    """撤回裸下版 finish：grab_by_ref + 同一份收尾。"""
    sha, err = await grab_by_ref(client, st, row)
    if err:
        return None, err
    part = st.abs(f"tmp/{row['file_uid']}.part")
    return _place(st, row, part, sha)


def _place(st, row, part, sha):
    """拿到 sha 之后的共同收尾（finish 与撤回裸下共用）。"""
    if st.get(row["msg_id"]) is None:
        # 下载期间这条被删了（页面上删的）：文件落盘也没人引用，白占磁盘，直接扔掉。
        # 不检查的话它会变成一份 used_bytes() 数不到的孤儿文件。
        _drop_part(st, row["file_uid"])
        return None, None
    ext = store_mod.ext_of(row["mime"])
    prev = st.by_sha(sha)
    if prev is not None and prev["path"] and os.path.exists(st.abs(prev["path"])):
        _drop_part(st, row["file_uid"])        # 内容一样，白下了，挂到老文件上
        blob_rel = prev["path"]
    else:
        prev = None
        blob_rel = store_mod.blob_rel(sha, ext)
        os.makedirs(os.path.dirname(st.abs(blob_rel)), exist_ok=True)
        os.replace(part, st.abs(blob_rel))

    link = make_link(st, row, st.abs(blob_rel), sha, row["mime"])
    # 被编辑过的行永远不算「重复」：编辑换上去的是**另一个**文件（新 file_uid），只是内容
    # 碰巧和别的行一样（典型：几条不同的图被作者陆续改成了同一张）。参考项目
    # （Telegram-bot-downloader bot.py:537）按 file_uid 去重，天然不会撞上这个。
    dup = None if row["edited"] else (prev["msg_id"] if prev is not None else None)
    st.set_done(row["msg_id"], sha, blob_rel, link, row["thumb"], dup_of=dup)
    # 入库时被挂到这条上的重复行，现在给它们也在各自作者目录里补个链接
    for d in st.dups_of(row["msg_id"]):
        st.set_link(d["msg_id"], make_link(st, d, st.abs(blob_rel), sha, d["mime"]))
    return row["size"] or 0, None


class BlobSession(SQLiteSession):
    """session 只在内存里跑，落盘的是加密后的整库镜像。

    为什么能这么接：Telethon 的 SQLiteSession 构造时第一个动作就是 _cursor()
    （sessions/sqlite.py:48），所以在父类去读 sessions 表之前把镜像灌进去就行；
    close() 对 :memory: 本来就是空操作（同文件 276），内存库会一直活着。
    这样 entities 缓存和 update_state 全保留，跟以前用文件完全等价，只是文件变成密文。
    """

    def __init__(self, reg, sealer):
        self._reg = reg
        self._sealer = sealer
        blob = reg.setting("session_blob", "")
        self._loaded = bool(blob)               # 读过镜像 → 以后每次都写回
        self._mirror = sealer.unseal(secure.b64d(blob)) if blob else None
        super().__init__(None)                  # session_id=None → filename = ':memory:'

    def _cursor(self):
        if self._conn is None:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            if self._mirror:
                self._conn.deserialize(self._mirror)
                self._mirror = None
        return self._conn.cursor()

    def save(self):
        super().save()
        # 全新空库、又还没登录：别写，否则「有没有 session」就判断不出来了
        if self._auth_key is None and not self._loaded:
            return
        self._reg.set_setting(
            "session_blob", secure.b64e(self._sealer.seal(self._conn.serialize())))

    def reseal(self, sealer):
        """换密封器（设/清口令）：按新密封器把当前内存库写一遍。"""
        self._sealer = sealer
        self.save()


class Watcher:
    """一个进程一份：登录状态、事件监听、下载线程、兜底扫描都在这里。"""

    def __init__(self, root, session_path, config, proxy=None):
        self.root = root
        self.cfg = config
        self.proxy = proxy
        self.session_file = (session_path if session_path.endswith(".session")
                             else session_path + ".session")
        self.reg = store_mod.Registry(root).open()
        self.sealer = secure.Sealer()       # 默认档：只用 DPAPI，重启自动解开
        self.cfg.sealer = self.sealer
        self.blob_session = None
        self.locked = False                 # 设了口令、等着在本机网页上解锁
        self.pw_fails = 0
        self.pw_until = 0.0
        self.stores = {}
        for cid in store_mod.chat_dirs(root):     # 启动时把已有的群库打开
            self.stores[cid] = store_mod.Store(root, cid).open()
        self.client = None          # 按需构造：没填 api_id 也先把服务起起来
        self.qr = None
        self.qr_task = None
        self.state = "starting"     # setup|login|connecting|offline|ok|need_password|expired|error
        self.error = None
        self.worker = None
        self.sweeper = None
        self.running = True
        self.hooked = False
        self.last_sweep = {}
        self.note = ""
        self._chats_cache = []
        self._chats_at = 0
        self._av_task = None        # 头像后台补下载协程
        self._busy_uids = set()     # 正在下载的 file_uid：同一文件绝不能两个协程同时下

    # --- 库 ---

    def store(self, chat_id):
        if chat_id not in self.stores:
            self.stores[chat_id] = store_mod.Store(self.root, chat_id).open()
        return self.stores[chat_id]

    def used_bytes(self):
        return sum(s.used_bytes() for s in self.stores.values())

    async def downloading(self):
        """正在下哪几条、各下到多少 —— 网页上给没下好的卡片显示进度用。

        看的是 tmp/ 里的 .part：文件名里的数字就是 file_uid，按它反查是哪条消息。
        async 是为了走 app.call 在事件循环线程上执行（SQLite 只能在那条线程上用）。
        """
        out = {}
        for st in list(self.stores.values()):
            try:
                names = os.listdir(st.d["tmp"])
            except OSError:
                continue
            for name in names:
                uid = name[:-5] if name.endswith(".part") else ""
                if not uid.isdigit():
                    continue
                try:
                    done = os.path.getsize(os.path.join(st.d["tmp"], name))
                except OSError:
                    continue
                row = st.by_uid(int(uid))
                if row is not None:
                    out[str(row["msg_id"])] = {"done": done, "total": row["size"] or 0}
        return out

    async def peer(self, chat_id):
        """拿 input entity 用来按 id 回捞消息。"""
        try:
            return await self.client.get_input_entity(chat_id)
        except Exception:
            await self.client.get_dialogs()
            return await self.client.get_input_entity(chat_id)

    async def chats(self, force=False):
        """账号里的群和频道（左栏那个列表），带上监视状态。

        iter_dialogs 对账号大的话很慢，所以带 5 分钟缓存，别每点一次都去问服务器。
        配过监视但不在会话列表里的也补进来，不然开关就找不到了。
        """
        now = time.time()
        if not force and self._chats_cache and now - self._chats_at < 300:
            return self._merge_watches(self._chats_cache)
        if self.client is None:                  # 还没登录：至少把已配监视的列出来
            return self._merge_watches([])
        out = []
        pun = []                                 # 没头像缓存的会话，稍后后台补
        keys = self.reg.avatar_keys()
        async for d in self.client.iter_dialogs(limit=500):
            e = d.entity
            if not isinstance(e, (Channel, Chat)):
                continue
            out.append({"chat_id": d.id, "title": d.name,
                        "kind": "频道" if getattr(e, "broadcast", False) else "群",
                        "noforwards": bool(getattr(e, "noforwards", False)),
                        "forum": bool(getattr(e, "forum", False))})
            if d.id not in keys:                 # 列表先走，头像后台补
                pun.append((d.id, e))
        self._chats_cache, self._chats_at = out, now
        self._avatar_backfill(pun)
        return self._merge_watches(out)

    def _avatar_backfill(self, pun):
        """后台逐个补头像（一次一批，上一批没做完不开新的）。"""
        if not pun or getattr(self, "_av_task", None) and not self._av_task.done():
            return
        self._av_task = asyncio.create_task(self._avatar_dl(pun))

    async def _avatar_dl(self, pun):
        for chat_id, e in pun:
            if self.client is None:
                return
            try:
                await self.avatar(e, chat_id)    # 头像：photo id 变了才重下，缓存免费
            except Exception as exc:             # 拉不到头像不影响列表本体
                print(f"  头像 {chat_id} 下载失败：{type(exc).__name__}", flush=True)
        self._av_task = None

    async def avatar(self, e, chat_id):
        """把 entity 的头像存成小 jpg。photo id 没变就跳过（几百个会话刷新时才不卡）。"""
        photo = getattr(e, "photo", None)   # Channel/Chat 的头像都是这个 ChatPhoto 对象
        if photo is None or isinstance(photo, ChatPhotoEmpty):
            return
        key = str(photo.photo_id)
        if self.reg.avatar_key(chat_id) == key:
            return
        buf = await self.client.download_profile_photo(e, file=bytes,
                                                       download_big=False)
        if not buf:
            return
        d = os.path.join(self.root, "avatars")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{chat_id}.jpg"), "wb") as f:
            f.write(buf)
        self.reg.set_avatar_key(chat_id, key)

    def _merge_watches(self, chats):
        by_id = {c["chat_id"]: dict(c) for c in chats}
        for w in self.reg.watches():
            c = by_id.setdefault(w["chat_id"], {"chat_id": w["chat_id"], "kind": "群",
                                                "noforwards": None, "forum": None})
            c["title"] = c.get("title") or w["title"] or str(w["chat_id"])
            c["watch"] = {"enabled": bool(w["enabled"]),
                          "kinds": [k for k in (w["kinds"] or "").split(",") if k],
                          "max_mb": w["max_mb"], "authors": w["authors"],
                          "topics": w["topics"], "started_at": w["started_at"]}
        keys = self.reg.avatar_keys()
        for c in by_id.values():
            c.setdefault("watch", None)
            if c["chat_id"] in keys:               # 有头像文件的才给 URL
                c["avatar"] = f"/api/avatar/{c['chat_id']}"
        # 开着监听的前排坐，其余（停了的、没配的）混在一起按标题
        return sorted(by_id.values(),
                      key=lambda c: (not (c["watch"] and c["watch"]["enabled"]),
                                     c["title"] or ""))

    def avatar_file(self, chat_id):
        """头像文件在不在（server 的 /api/avatar 用）。"""
        p = os.path.join(self.root, "avatars", f"{chat_id}.jpg")
        return p if os.path.isfile(p) else None

    # --- 客户端与登录 ---

    def has_session(self):
        """内存里有没有可用的 auth_key。密文在不在是另一回事 —— 锁着的时候不解密。"""
        if self.client is None:
            return False
        return getattr(self.client.session, "auth_key", None) is not None

    def ensure_client(self):
        """按需构造客户端。没填 api_id 就先返回 None（hash 可以是占位值）。"""
        if self.client is None and self.cfg.ready and not self.locked:
            self.client = TelegramClient(self._blob_session(), self.cfg.api_id,
                                         self.cfg.api_hash, proxy=self.proxy)
        return self.client

    def _blob_session(self):
        self.blob_session = BlobSession(self.reg, self.sealer)
        return self.blob_session

    async def _drop_client(self):
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
        self.hooked = False
        self.qr = None

    async def boot(self):
        """启动/重试：有 session 就直接连上，别一上来就让人扫码。

        三条分支：
          设了口令、这个进程还没解锁 → 锁着等解锁（不建客户端、不连 Telegram、不起下载）
          没填 api_id → setup
          否则 → 用加密的 session 连上；连不上就看状态里的原因
        """
        self.error = None
        self.locked = False
        # 锁不锁只跟口令有关，跟 api_id 填没填无关 —— 凭证该锁的时候就得锁
        if self.reg.setting("pw_salt", "") and not self.sealer.by_passcode:
            self.locked = True
            self.state = "locked"
            return
        if not self.cfg.ready:
            self.state = "setup"
            return
        self.cfg.migrate_plaintext(self.sealer)   # 老版本留下的明文 api_hash 就地加密
        try:
            client = self.ensure_client()
        except OSError as exc:                    # 换过 Windows 账户：DPAPI 解不开
            self.state = "error"
            self.error = f"{exc}。登录凭证得重新登录一次（设置里可以重置）。"
            return
        if not self.has_session():
            self.state = "login"
            return
        self.state = "connecting"
        try:
            await tg.connect(client)
            if not await client.is_user_authorized():
                self.state = "login"
                return
            # is_user_authorized() 只看本地：auth_key 被服务器注销后它照样返回 True，
            # 于是后面每一步都莫名其妙地失败。所以再真问一次服务器。
            await client.get_me()
            self.state = "ok"
            await self.start()
        except Exception as exc:
            if type(exc).__name__ in DEAD_SESSION:
                self.state = "login"
                self.error = "session 已失效，需要重新登录"
                return
            self.state = "offline"
            self.error = hint(exc)

    # --- 凭证加密 / 口令 ---

    def _set_sealer(self, sealer):
        """换密封器：内存里的 session 库、api_hash、Watcher 自己，三处一起换。"""
        if self.blob_session is not None:
            self.blob_session.reseal(sealer)
        self.cfg.reseal(sealer)
        self.sealer = sealer

    def _check_pw(self, sealer):
        """口令对不对：拿 pw_check 这个固定小串试一下，不依赖 session 在不在。"""
        want = b"tgjt-lock"
        blob = self.reg.setting("pw_check", "")
        if not blob:
            return True                       # 没留校验串（老状态），只能靠 session 本身
        try:
            return sealer.unseal(secure.b64d(blob)) == want
        except OSError:
            return False

    async def unlock(self, passcode):
        """本机网页上输口令：派生 → 试解 → 成功就连上。错太多次拖一下。"""
        now = time.time()
        if now < self.pw_until:
            return {"ok": False, "error": f"错太多次了，{int(self.pw_until - now) + 1} 秒后再试"}
        salt = self.reg.setting("pw_salt", "")
        if not salt:
            return {"ok": False, "error": "没设过口令，不用解锁"}
        sealer = secure.Sealer(passcode, secure.b64d(salt))
        ok = self._check_pw(sealer)
        if ok:
            blob = self.reg.setting("session_blob", "")
            try:                              # session 密文也顺手验一下，坏了要说清楚
                if blob:
                    sealer.unseal(secure.b64d(blob))
            except OSError:
                return {"ok": False, "error": "口令对，但 session 密文解不开：可能换过 Windows 账户。"
                                              "走「重置」重新登录。"}
        if not ok:
            self.pw_fails += 1
            if self.pw_fails >= 5:
                self.pw_fails = 0
                self.pw_until = time.time() + 30
            return {"ok": False, "error": "口令不对"}
        self.pw_fails, self.pw_until = 0, 0.0
        self._set_sealer(sealer)
        self.cfg.migrate_plaintext(self.sealer)   # 还没加密的明文 api_hash 跟着一起封
        self.locked = False
        await self._drop_client()
        await self.boot()
        return {"ok": True, "status": await self.status()}

    async def set_passcode(self, passcode):
        """设口令：session 密文和 api_hash 都用口令派生的熵重封一遍。"""
        if self.locked:
            return {"ok": False, "error": "先解锁"}
        if len(passcode or "") < 6:
            return {"ok": False, "error": "口令至少 6 位。忘了只能重新登录，别设得太随意。"}
        salt = secure.new_salt()
        self._set_sealer(secure.Sealer(passcode, salt))
        self.reg.set_setting("pw_salt", secure.b64e(salt))
        self.reg.set_setting("pw_check", secure.b64e(self.sealer.seal(b"tgjt-lock")))
        return {"ok": True}

    async def clear_passcode(self):
        """取消口令：退回只用 DPAPI（重启自动解开，但同用户木马能解）。"""
        if self.locked:
            return {"ok": False, "error": "先解锁"}
        self._set_sealer(secure.Sealer())
        self.reg.del_setting("pw_salt")
        self.reg.del_setting("pw_check")
        return {"ok": True}

    async def reset_session(self):
        """忘口令的逃生口：把密文扔掉、回到重新登录。不需要口令 —— 否则人被锁死。

        扔掉的只是密文，攻击者拿到没收益；代价是 api_hash 也要重填一次。
        """
        await self._drop_client()             # 先断（它会写一次密文），再删
        self.blob_session = None
        for key in ("session_blob", "api_hash_enc", "api_hash", "pw_salt", "pw_check"):
            self.reg.del_setting(key)
        self.sealer = secure.Sealer()
        self.cfg.sealer = self.sealer
        self.locked = False
        self.pw_fails, self.pw_until = 0, 0.0
        self.error = None
        await self.boot()
        return await self.status()

    async def retry(self):
        self.error = None
        await self.boot()
        return await self.status()

    async def set_config(self, api_id, api_hash):
        """网页上填的 api_id/api_hash：存本地，然后现场重建客户端并试连。"""
        self.cfg.save(api_id, api_hash)
        await self._drop_client()
        self.error = None
        await self.boot()
        return await self.status()

    async def status(self):
        """给网页看的。只报告 api_hash 有没有设过，绝不回明文。

        局域网那两个字段也从这里出：库连接是 loop 线程的，HTTP 线程直接读会抛
        「SQLite objects created in a thread can only be used in that same thread」。
        """
        return {"state": self.state, "error": self.error,
                "connected": bool(self.client and self.client.is_connected()),
                "has_config": self.cfg.ready, "has_session": self.has_session(),
                "api_id": self.cfg.api_id, "api_hash_set": self.cfg.api_hash_set,
                "note": self.note,
                "locked": self.locked,
                "passcode_set": bool(self.reg.setting("pw_salt", "")),
                # 旧的明文 session 文件还在不在磁盘上（已经不用它了，只是提醒删掉）
                "legacy_file": os.path.isfile(self.session_file),
                "lan_password_set": bool(self.reg.setting("lan_hash", ""))}

    def login_ready(self):
        """能不能重新登录。扫码和手机号登录协议上都必须带 api_hash（qrlogin.py:19），
        所以 session 死了而 hash 没填时，得先让人把 hash 补上。能登返回 None。"""
        if not self.cfg.ready:
            return "还没填 api_id"
        if not self.cfg.api_hash_set:
            return "重新登录要带 api_hash（协议要求），请先在设置里填上"
        return None

    async def login_qr(self):
        why = self.login_ready()
        if why:
            self.state = "setup"
            return {"state": "setup", "error": why}
        client = self.ensure_client()
        await tg.connect(client)
        if self.qr is not None and time.time() < self.qr.expires.timestamp():
            return self._qr_payload()
        self.qr = await client.qr_login()
        self.state = "waiting"
        self.error = None
        self.qr_task = asyncio.create_task(self._qr_wait())
        return self._qr_payload()

    def _qr_payload(self):
        img = qrcode.make(self.qr.url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return {"state": self.state, "url": self.qr.url,
                "png": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
                "expires": int(self.qr.expires.timestamp())}

    async def _qr_wait(self):
        try:
            await self.qr.wait()
            self.client.session.save()   # auth_key 只写在内存库里，落盘得自己叫一次
            self.state = "ok"
            await self.start()
        except SessionPasswordNeededError:
            self.state = "need_password"
        except asyncio.TimeoutError:
            self.state = "expired"
        except Exception as exc:
            self.state = "error"
            self.error = f"{type(exc).__name__}: {exc}"

    async def login_password(self, password):
        why = self.login_ready()
        if why:
            return {"ok": False, "error": why}
        try:
            await self.client.sign_in(password=password)
            self.client.session.save()   # 同上：登录成功后立刻把密文落盘
            self.state = "ok"
            await self.start()
            return {"ok": True}
        except PasswordHashInvalidError:
            return {"ok": False, "error": "密码不对。是「设置 → 隐私和安全 → 两步验证」那个密码。"}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # --- 监听 ---

    async def start(self):
        """登录成功后挂上事件、下载线程、兜底扫描。重复调用是安全的。"""
        if self.client is None:
            return
        if not self.hooked:
            self.client.add_event_handler(self._on_new, events.NewMessage)
            self.client.add_event_handler(self._on_edit, events.MessageEdited)
            self.client.add_event_handler(self._on_delete, events.MessageDeleted)
            self.hooked = True
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._download_loop())
        if self.sweeper is None or self.sweeper.done():
            self.sweeper = asyncio.create_task(self._sweep_loop())
        self.note = ""

    async def _on_new(self, event):
        """推送来了。只处理开着监听的群，且只处理符合类型/大小的。"""
        chat_id = event.chat_id
        w = self.reg.watch(chat_id)
        if not w or not w["enabled"]:
            return
        try:
            await self._take(chat_id, event.message, w, thumb=True)
        except Exception as exc:
            print(f"[{chat_id}] 收消息出错：{type(exc).__name__}: {exc}", flush=True)

    async def _on_edit(self, event):
        """编辑不改消息 ID，所以是原地更新同一行 —— 卡片不会变成两条。"""
        chat_id = event.chat_id
        w = self.reg.watch(chat_id)
        if not w or not w["enabled"]:
            return
        try:
            await self._revise(chat_id, event.message, w)
        except Exception as exc:
            print(f"[{chat_id}] 处理编辑出错：{type(exc).__name__}: {exc}", flush=True)

    async def _on_delete(self, event):
        """消息被删了：只打标记。本地文件留不留是网页上那个删除按钮的事。"""
        chat_id = event.chat_id
        if chat_id is None:
            return                       # 更新里没带 peer，认不出是哪个群，只能放弃
        w = self.reg.watch(chat_id)
        if not w or not w["enabled"]:
            return
        try:
            st = self.store(chat_id)
            for mid in (event.deleted_ids or []):
                if st.get(mid) is not None:
                    st.mark_deleted(mid)
        except Exception as exc:
            print(f"[{chat_id}] 处理删除出错：{type(exc).__name__}: {exc}", flush=True)

    async def _revise(self, chat_id, m, w):
        """把一条编辑过的消息同步进库。返回落库状态。

        只有我们显示的东西真的变了，才算一次编辑（改了说明、换了媒体、媒体被删掉）。
        Telegram 也会为我们看不见的东西发编辑更新（比如作者改了内联按钮）—— 那种既不该
        在卡片上标「已编辑」，也不该留下任何痕迹，只把 edit_seen 水位推上去就行。
        """
        st = self.store(chat_id)
        row = st.get(m.id)
        if row is None:
            return await self._take(chat_id, m, w, thumb=True)   # 编辑之后才带上媒体
        if not is_new_edit(row, m):
            # 迟到的编辑事件：edit_date 没比 edit_seen/added 晚 = 入库**之前**就发生的编辑
            #（待机恢复/重连回放一批更新时常见，线上 10-18 的假「已编辑」就是这么标上的。
            # 兜底扫描一直有这道闸，实时路径漏了）。跟扫描同口径：只推水位，不动卡片。
            if m.edit_date:
                st.set_status(m.id, row["status"], row["note"],
                              edit_seen=int(m.edit_date.timestamp()))
                st.commit()
            return row["status"]
        kinds = [k for k in (w["kinds"] or "").split(",") if k]
        info, _why = classify(m)
        topic_id = tg.topic_of(m)
        when = int(m.edit_date.timestamp()) if m.edit_date else int(time.time())
        media_gone = info is None or bool(kinds and info["kind"] not in kinds)
        media_changed = (not media_gone) and info["file_uid"] != row["file_uid"]
        text_changed = (m.message or "") != (row["caption"] or "")

        if not (media_gone or media_changed or text_changed):
            st.set_status(m.id, row["status"], row["note"], edit_seen=when)
            st.commit()
            return row["status"]

        base = {"edited": when, "edit_seen": when, "topic_id": topic_id,
                "caption": m.message, "caption_before": row["caption"],
                "permalink": tg.permalink_id(chat_id, m.id, topic_id)}

        if media_gone:
            # 媒体被编辑掉了：库里那份留着（归档的就是当时发出来的东西），只更新文字
            st.set_status(m.id, row["status"], "编辑后媒体已不在消息里", **base)
            return row["status"]

        if not media_changed:
            st.set_status(m.id, row["status"], None, **base)      # 最常见的：只改了说明
            return row["status"]

        # 媒体被换掉了：先把**当前这份**存成一条历史版本（多次编辑就是多条，seq 递增）。
        # 从此这份旧文件不会再被删 —— drop_files 的引用计数把 revisions 也算进去了，
        # 所以「编辑不删内容」是数据层保证的，不靠这里的小心。
        st.add_revision(m.id, when)
        # 旧文件、旧缩略图、旧硬链接**先留着** —— 新版下好之前卡片还能点开看/播。
        # 新版落地时 set_done 才按引用计数清旧的。（以前是这里立刻删，结果新版下好之前卡片
        # 只剩一句占位文字，「已编辑的点不开」就是这么来的。）
        over = bool(info["size"] and info["size"] > w["max_mb"] * MiB)
        # 旧 uid 的半成品（tmp 里的 .part/.prog）没人会来收了：新版本是另一个 uid、
        # 另一份 .part。正在被别的协程下着的 uid 别动。缩略图由 revisions 引用计数保活。
        if row["file_uid"] not in getattr(self, "_busy_uids", ()):
            _drop_part(st, row["file_uid"])
        st.set_status(m.id, "skipped" if over else "pending",
                      f"超过 {w['max_mb']} MB" if over else None,
                      kind=info["kind"], size=info["size"], mime=info["mime"],
                      width=info["width"], height=info["height"],
                      duration=info["duration"], grouped_id=m.grouped_id,
                      file_uid=info["file_uid"], dup_of=None, tries=0, **base)
        if not over:
            t = await grab_thumb(self.client, st, m, {"file_uid": info["file_uid"]})
            if t is not None:
                st.set_status(m.id, "pending", None, thumb=t)
        return "skipped" if over else "pending"

    def relink(self, chat_id, msg_id):
        """按当前行重建作者目录里的硬链接。历史版本被顶上来当当前版本之后要调一次，
        否则那份文件在 by-author 目录里会缺一个名字（内容还在，靠 files/ 那边引用）。"""
        st = self.store(chat_id)
        row = st.get(msg_id)
        if row is None or not row["path"]:
            return
        blob = st.abs(row["path"])
        if not os.path.exists(blob):
            return
        try:
            st.set_link(msg_id, make_link(st, row, blob, row["sha256"], row["mime"]))
        except OSError:
            pass

    async def _take(self, chat_id, m, w, thumb=True):
        """把一条消息按规则收进库。返回落库的状态，None 表示这条不要。"""
        if not allowed(w, m.sender_id):
            return None
        tw = [t for t in (w["topics"] or "").split(",") if t]
        if tw:                                   # 按话题监听（论坛群）：名单外不收；
            tkey = tg.topic_of(m)                # 话题和作者是「且」，两个都写了都须过
            tkey = str(tkey) if tkey else "general"
            if tkey not in tw:
                return None
        kinds = [k for k in (w["kinds"] or "").split(",") if k]
        info, why = classify(m)
        if info is None or (kinds and info["kind"] not in kinds):
            return None

        st = self.store(chat_id)
        existing = st.get(m.id)
        if existing is not None:
            # 兜底扫描重看活消息时顺手补票：旧行（列没家的）也获得撤回后裸下能力。
            # 只认媒体对象还是这条原来的那个 —— 被编辑换掉的不算旧媒体的票。
            if not existing["media_ref"] and info.get("ref") \
                    and info["file_uid"] == existing["file_uid"]:
                st.set_media_ref(m.id, info["ref"])
            return existing["status"]              # 已经收过了（兜底扫描会重复看到同一条）
        topic_id = tg.topic_of(m)
        st.author_dir(m.sender_id, tg.author_name(m))
        row = {"chat_id": chat_id, "msg_id": m.id, "topic_id": topic_id,
               "author_id": m.sender_id, "date": int(m.date.timestamp()),
               "kind": info["kind"], "size": info["size"], "mime": info["mime"],
               "width": info["width"], "height": info["height"],
               "duration": info["duration"], "caption": m.message,
               "permalink": tg.permalink_id(chat_id, m.id, topic_id),
               "grouped_id": m.grouped_id, "file_uid": info["file_uid"],
               "media_ref": info.get("ref", ""),
               "tries": 0, "added": int(time.time())}

        if info["size"] and info["size"] > w["max_mb"] * MiB:
            row.update(status="skipped", note=f"超过 {w['max_mb']} MB")
        else:
            prev = st.by_uid(info["file_uid"])
            if prev is None:
                row.update(status="pending")
            elif prev["status"] == "done":
                # 同一个文件又发了一遍：不重复下载，直接挂到已经存好的那份上
                row.update(status="done", sha256=prev["sha256"], path=prev["path"],
                           thumb=prev["thumb"], dup_of=prev["msg_id"])
                if prev["path"] and os.path.exists(st.abs(prev["path"])):
                    row["link"] = make_link(st, row, st.abs(prev["path"]),
                                            prev["sha256"], prev["mime"])
            else:
                row.update(status="dup", dup_of=prev["msg_id"])

        is_new = st.insert(row)
        st.commit()
        if is_new and thumb and row["status"] == "pending":
            t = await grab_thumb(self.client, st, m, row)
            if t is not None:
                st.set_status(m.id, "pending", None, thumb=t)
                st.commit()
        return row["status"]

    # --- 下载线程 ---

    async def _download_loop(self):
        while self.running:
            try:
                did = await self._drain()
            except Exception as exc:
                # 走到这里说明是程序 bug（每条自己已经兜过异常了），不是网络问题
                print(f"下载线程出错（程序 bug，请把这一行发给开发者）："
                      f"{type(exc).__name__}: {exc}", flush=True)
                did = 0
            nap = 2 if did else 8
            t0 = time.monotonic()
            await asyncio.sleep(nap)
            frozen = frozen_seconds(t0, nap, time.monotonic())
            if frozen >= FROZEN_S:
                print(f"本进程刚被挂起过约 {frozen:.0f} 秒（系统进了待机，那会儿手机连不上）",
                      flush=True)

    async def _drain(self):
        """下一批。入库不满 ALBUM_SETTLE 的先放着，让相册成员到齐一起下。

        同时最多下 DOWNLOAD_CONCURRENCY 条；每条自己兜异常 —— 一条下不动只影响它自己，
        后面排队的照常走。以前这里一处没兜住的异常就把整轮带走，后面的消息永远轮不到。
        """
        cap = self.reg.disk_cap()
        used = self.used_bytes()
        now = time.time()
        sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
        did = 0
        for chat_id, st in list(self.stores.items()):
            # 不看 watch 的开关：停掉监听之后，队列里已经排上的还是下完更好
            ready = [r for r in st.pending("full", 60)
                     if now - (r["added"] or 0) >= ALBUM_SETTLE]
            if not ready:
                continue
            try:
                peer = await self.peer(chat_id)
            except Exception as exc:
                print(f"[{chat_id}] 拿不到 peer：{type(exc).__name__}", flush=True)
                continue
            todo = []
            for row in ready:
                if cap and used + (row["size"] or 0) > cap:
                    st.set_status(row["msg_id"], "pending", "超过磁盘上限，先只记索引")
                    st.commit()
                    self.note = f"磁盘占用已达上限（{used / (1 << 30):.1f} GB），只记索引不下载"
                    break
                used += row["size"] or 0           # 先占上额度，免得并发的几条一起超上限
                todo.append(row)
            if not todo:
                continue
            got = await asyncio.gather(*[self._one(st, peer, row, sem, now) for row in todo])
            did += sum(got)
            st.commit()
        return did

    async def _one(self, st, peer, row, sem, now):
        """同 file_uid 的文件全局同时只下一个（相册里内容相同的几条共用一个 .part）。"""
        uid = row["file_uid"]
        if uid in self._busy_uids:   # 别的协程正在下这份内容：这轮跳过，下轮再说
            return 0
        self._busy_uids.add(uid)
        try:
            return await self._one_impl(st, peer, row, sem, now)
        finally:
            self._busy_uids.discard(uid)

    async def _one_impl(self, st, peer, row, sem, now):
        """下一条。任何异常都收在这里，绝不让它把整轮带走。返回是否真下好了。"""
        async with sem:
            try:
                msgs = await get_by_ids(self.client, peer, [row])
                msg = msgs[0] if msgs else None
                if msg is None and not row["media_ref"]:
                    st.set_status(row["msg_id"], "failed", "消息已经不在了（被删或撤回）")
                    return 0
                # 给每条下载一个上限：没有超时的话，一条卡住的连接会把整个队列堵死
                t0 = time.time()                   # 速率基线：调下载优化前后都从 _run.log 里比（B 计划）
                if msg is None:
                    # 消息被撤了，但入库时存过位置：blob 还在服务器上，凭票直下。
                    _size, err = await asyncio.wait_for(
                        finish_raw(self.client, st, row),
                        timeout=120 + (row["size"] or 0) / 10240)
                else:
                    _size, err = await asyncio.wait_for(
                        finish(self.client, st, msg, row),
                        timeout=120 + (row["size"] or 0) / 10240)
            except asyncio.TimeoutError:
                err = "下载超时（连接卡住了），等会儿再试"
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
            if not err and (row["size"] or 0):
                dt = max(time.time() - t0, 0.05)
                mb = row["size"] / MiB
                print(f"  [{row['msg_id']}] {mb:.1f} MB / {dt:.1f}s = {mb / dt:.2f} MB/s",
                      flush=True)
            if err:
                if err == REF_GONE:
                    st.set_status(row["msg_id"], "failed", err)
                    print(f"  [{row['msg_id']}] {err}", flush=True)
                    return 0
                if err == MEDIA_GONE:
                    st.set_status(row["msg_id"], "skipped", err)
                    print(f"  [{row['msg_id']}] {err}", flush=True)
                    return 0
                # 别长退避：30s/60s/120s/… 的等待给「消息被删/被编辑」留了时间窗，下到一半
                # 的文件等几分钟再回去多半就引用过期了。前 3 次失败**不设等待**（清掉
                # next_try，set_status 不写残留旧值）：下载循环下一轮（2～8s）直接接着
                # .part 续传。连着失败多半是网络真的断了/服务端在踢连接，第 4 次起才拉开
                # 间隔（60s 起翻倍、600s 封顶），别空转打 Telegram。
                # FLOOD_WAIT 是服务端限流不是故障：不计次，等它要的秒数（比瞎退避准）。
                flood = err.startswith("FLOOD_WAIT")
                tries = (row["tries"] or 0) + 1 if not flood else (row["tries"] or 0)
                if not flood and tries >= DOWNLOAD_MAX_TRIES:
                    st.set_status(row["msg_id"], "failed", err)
                    print(f"  [{row['msg_id']}] 反复 {tries} 次没下成，标记下载失败：{err}",
                          flush=True)
                    return 0
                if flood:
                    try:
                        wait = min(600, int(err.split("FLOOD_WAIT ")[1].split("s")[0]) + 1)
                    except (IndexError, ValueError):
                        wait = 30
                    st.set_status(row["msg_id"], "pending", err, tries=tries,
                                  next_try=int(now + wait))
                    print(f"  [{row['msg_id']}] {err}（{wait}s 后再试）", flush=True)
                elif tries <= 3:
                    st.set_status(row["msg_id"], "pending", err, tries=tries, next_try=None)
                    print(f"  [{row['msg_id']}] {err}（下轮直接重试）", flush=True)
                else:
                    wait = min(600, 60 * (2 ** min(tries - 4, 3)))
                    st.set_status(row["msg_id"], "pending", err, tries=tries,
                                  next_try=int(now + wait))
                    print(f"  [{row['msg_id']}] {err}（{wait}s 后再试）", flush=True)
                return 0
            self.note = ""
            return 1

    # --- 兜底扫描 ---

    async def _sweep_loop(self):
        while self.running:
            await asyncio.sleep(SWEEP_SECONDS)
            for w in self.reg.enabled():
                try:
                    await self.sweep(w["chat_id"])
                except Exception as exc:
                    print(f"兜底扫描出错：{type(exc).__name__}: {exc}", flush=True)
            self.export_all()

    def export_static(self, chat_id):
        """顺手生成一份静态 HTML：关掉程序也能看图看视频。"""
        st = self.store(chat_id)
        rows = st.gallery_rows()
        if not rows:
            return 0
        # 和网页端同一个口径：换过媒体的行也要在离线页里展开看历史版本
        revs = {r["msg_id"]: st.revisions(r["msg_id"]) for r in rows if r["edited"]}
        w = self.reg.watch(chat_id)
        title = (w["title"] if w and w["title"] else None) or str(chat_id)
        return gallery.render(st.d["html"], rows, title, chat_id, revs)

    def export_all(self):
        for chat_id in list(self.stores):
            try:
                self.export_static(chat_id)
            except Exception as exc:
                print(f"静态导出失败：{type(exc).__name__}: {exc}", flush=True)

    async def sweep(self, chat_id):
        """从游标往回翻，把监听期间漏掉的补上。"""
        st = self.store(chat_id)
        peer = await self.peer(chat_id)
        cursor = st.cursor()
        if cursor == 0:                            # 第一次：从现在算起，不补历史
            newest = await fetch_page(self.client, peer, 0, 1)
            st.set_cursor(newest[0].id if newest else 0)
            self.last_sweep[chat_id] = time.time()
            return 0
        w = self.reg.watch(chat_id)
        msgs = await fetch_pages(
            lambda before, limit: fetch_page(self.client, peer, before, limit), cursor)
        topics = set()
        for m in msgs:
            row = st.get(m.id)
            if is_new_edit(row, m):
                await self._revise(chat_id, m, w)      # 程序没开时错过的编辑
                topics.add(tg.topic_of(m))
                continue
            if await self._take(chat_id, m, w, thumb=True):
                topics.add(tg.topic_of(m))
        if msgs:
            st.set_cursor(max(m.id for m in msgs))
        if topics:
            await resolve_topics(self.client, peer, st, sorted(t for t in topics if t))
        self.last_sweep[chat_id] = time.time()
        return len(msgs)

    async def backfill(self, chat_id, hours):
        """手动补最近 N 小时。

        **绝不碰游标**：游标是兜底扫描用来查缺口的水位线，一旦被补历史推上去，
        漏消息的保护就废了。所以这里独立地按时间线往回翻，撞到时间线就停。
        """
        if self.client is None:
            return 0
        st = self.store(chat_id)
        peer = await self.peer(chat_id)
        w = self.reg.watch(chat_id) or self.reg.set_watch(
            chat_id, kinds="photo,video", max_mb=100)
        cutoff = time.time() - max(0.1, hours) * 3600
        added, before, pages = 0, 0, 0
        while pages < 200:
            msgs = await fetch_page(self.client, peer, before, 100)
            if not msgs:
                break
            pages += 1
            for m in msgs:
                if m.date.timestamp() < cutoff:
                    continue
                if await self._take(chat_id, m, w, thumb=True):
                    added += 1
            if msgs[-1].date.timestamp() < cutoff:     # 撞到时间线了
                break
            before = msgs[-1].id
            if len(msgs) < 100:
                break
        return added

    # --- 收尾 ---

    async def close(self):
        self.running = False
        for task in (self.worker, self.sweeper):
            if task is not None:
                task.cancel()
        self.export_all()                          # 关之前把静态版刷一遍
        for st in self.stores.values():
            st.close()
        self.reg.close()
        try:
            await self.client.disconnect()
        except Exception:
            pass


async def get_by_ids(client, peer, rows):
    msgs = await client.get_messages(peer, ids=[r["msg_id"] for r in rows])
    if not isinstance(msgs, list):
        msgs = [msgs]
    return list(msgs)
