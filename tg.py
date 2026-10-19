"""Telethon 的几件小事：连接、认目标、算永久链接。

刻意不 import probe.py：归档器要能独立长期跑，探针随时可以删。
"""

import asyncio
import re

from telethon.errors import ChannelPrivateError
from telethon.tl.types import Channel, Chat

DEFAULT_PROXY = "socks5h://127.0.0.1:10808"


def proxy_dict(url):
    """socks5h://127.0.0.1:10808 -> Telethon 的 proxy 参数。

    socks5h 是 curl 的叫法，Telethon 只认 socks5，远端 DNS 靠 rdns=True 表达。
    """
    if not url:
        return None
    m = re.match(r"^(socks5h|socks5|http)://(?:([^:@]+):([^@]+)@)?([^:/]+):(\d+)$",
                 url.strip(), re.I)
    if not m:
        raise ValueError(f"代理写法看不懂：{url}")
    scheme, user, pwd, host, port = m.groups()
    d = {"proxy_type": "socks5" if scheme.lower().startswith("socks") else "http",
         "addr": host, "port": int(port), "rdns": scheme.lower().endswith("h")}
    if user:
        d["username"], d["password"] = user, pwd
    return d


async def connect(client, attempts=6, delay=5):
    """握手期的 EOF 自己兜住。

    Telethon 一次都不重试它：asyncio.IncompleteReadError 继承自 EOFError 而不是 OSError，
    mtprotosender 那两处 except 都接不到（mtprotosender.py:259/:326）。
    """
    for i in range(1, attempts + 1):
        try:
            await client.connect()
            return
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
            if i == attempts:
                raise
            print(f"连接失败（{type(exc).__name__}: {exc}），"
                  f"{delay} 秒后第 {i + 1}/{attempts} 次", flush=True)
            await asyncio.sleep(delay)


def parse_target(raw):
    """-100… / 裸正数 / t.me/c/N / t.me/c/N/M -> 带 -100 前缀的 int；其余原样交给 Telethon。"""
    s = raw.strip()
    m = re.search(r"t\.me/c/(\d+)(?:/\d+)?", s)
    if m:
        return -10 ** 12 - int(m.group(1))
    if re.fullmatch(r"\d+", s):
        return -10 ** 12 - int(s)
    if re.fullmatch(r"-\d+", s):
        return int(s)
    return s


async def resolve(client, target):
    """先直接问；冷 session 里问不到就拉一次 dialogs 再问。"""
    ent = None
    for attempt in (1, 2):
        try:
            ent = await client.get_entity(target)
        except (ValueError, ChannelPrivateError, TypeError):
            ent = None
        if ent is not None or attempt == 2:
            break
        await client.get_dialogs()
    if ent is None:
        return None
    return ent if isinstance(ent, (Channel, Chat)) else None


def permalink_id(chat_id, msg_id, topic_id=None):
    """只用 id 拼永久链接，不需要 entity。

    私密群只能用 t.me/c；话题消息带上 thread_id，点开就落在对应话题里（links.md:250）。
    对成员来说这个写法对公开频道一样有效，所以统一用它。
    基础群（-123… 那种）没有这种链接，返回 None。
    """
    raw = -chat_id - 10 ** 12
    if raw <= 0:
        return None
    if topic_id:
        return f"https://t.me/c/{raw}/{topic_id}/{msg_id}"
    return f"https://t.me/c/{raw}/{msg_id}"


def topic_of(m):
    """消息属于哪个话题，None 表示 General（id=1）。

    实测（本群 300 条）：话题里的普通发帖是 reply_to_msg_id = 话题号、reply_to_top_id 空；
    只有在话题内回复某条消息时才出现 reply_to_top_id（forum.md:120/:131）。
    所以两个都认，优先 top。
    """
    rt = getattr(m, "reply_to", None)
    if rt is None:
        return None
    return getattr(rt, "reply_to_top_id", None) or getattr(rt, "reply_to_msg_id", None)


def author_name(m):
    """作者昵称。Telethon 的 m.sender 只是读缓存，不发请求。"""
    s = getattr(m, "sender", None)
    if s is None:
        return None
    name = " ".join(x for x in (getattr(s, "first_name", None),
                                getattr(s, "last_name", None)) if x)
    return name or getattr(s, "title", None) or getattr(s, "username", None)
