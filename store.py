"""归档库：SQLite 索引 + 内容寻址路径 + 去重查询。

一个群一个库目录：

    library/<chat_id>/files/<sha前2位>/<sha256>.<ext>        真身，按内容命名，天然去重
    library/<chat_id>/by-author/<作者id>_<昵称>/<日期>_<msgid>_<kind>_<sha8>.<ext>
                                                             硬链接，按作者翻文件用
    library/<chat_id>/thumbs/<file_uid>.jpg                  缩略图，先落这个，画廊立刻能用
    library/<chat_id>/tmp/<file_uid>.part                    半成品，断了下次接着下
    library/<chat_id>/index.db                               索引
    library/<chat_id>/index.html                             画廊
    library/index.db                                         全局小库：监视配置、磁盘上限、api_id

库里存的一律是相对路径（相对 library/<chat_id>），整个目录搬走也不会坏。
"""

import os
import re
import shutil
import sqlite3
import time

import secure

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS authors (
  chat_id INTEGER NOT NULL, author_id INTEGER NOT NULL,
  name TEXT, dir TEXT NOT NULL, first_seen INTEGER,
  PRIMARY KEY (chat_id, author_id));
CREATE TABLE IF NOT EXISTS topics (
  chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, title TEXT,
  PRIMARY KEY (chat_id, topic_id));
CREATE TABLE IF NOT EXISTS messages (
  chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL,
  topic_id INTEGER, author_id INTEGER, date INTEGER NOT NULL,
  kind TEXT NOT NULL, size INTEGER, mime TEXT, width INTEGER, height INTEGER,
  duration REAL, caption TEXT, permalink TEXT, grouped_id INTEGER,
  file_uid INTEGER, sha256 TEXT, path TEXT, link TEXT, thumb TEXT,
  status TEXT NOT NULL, note TEXT, dup_of INTEGER, tries INTEGER DEFAULT 0,
  added INTEGER DEFAULT 0, edited INTEGER, deleted INTEGER, star INTEGER,
  PRIMARY KEY (chat_id, msg_id));
CREATE INDEX IF NOT EXISTS idx_author ON messages(chat_id, author_id, date);
CREATE INDEX IF NOT EXISTS idx_uid ON messages(chat_id, file_uid);
CREATE INDEX IF NOT EXISTS idx_sha ON messages(sha256);
CREATE INDEX IF NOT EXISTS idx_status ON messages(chat_id, status, date);
CREATE TABLE IF NOT EXISTS revisions (
  chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL, seq INTEGER NOT NULL,
  kind TEXT, size INTEGER, mime TEXT, width INTEGER, height INTEGER, duration REAL,
  caption TEXT, sha256 TEXT, path TEXT, thumb TEXT, edited_at INTEGER,
  PRIMARY KEY (chat_id, msg_id, seq));
CREATE INDEX IF NOT EXISTS idx_rev_sha ON revisions(chat_id, sha256);
"""

REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
  chat_id INTEGER PRIMARY KEY, title TEXT,
  kinds TEXT NOT NULL DEFAULT 'photo,video',
  authors TEXT NOT NULL DEFAULT '',
  max_mb INTEGER NOT NULL DEFAULT 100,
  enabled INTEGER NOT NULL DEFAULT 0,
  started_at INTEGER, added_at INTEGER);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp",
       "image/gif": "gif", "image/bmp": "bmp", "image/heic": "heic",
       "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
       "video/x-matroska": "mkv", "video/3gpp": "3gp"}

_BAD = set('<>:"/\\|?*#%')
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _unlink(path):
    """删文件，不存在就当成功。返回是否真的删掉了。"""
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def safe_name(s, limit=24):
    """Windows 文件名/目录名消毒：非法字符、保留名、结尾的点空格。"""
    s = "".join("_" if (c in _BAD or ord(c) < 32) else c for c in (s or ""))
    s = re.sub(r"\s+", " ", s)[:limit].strip(" .")
    if not s:
        return "_"
    if s.upper() in _RESERVED or s.split(".")[0].upper() in _RESERVED:
        return "_" + s
    return s


def ext_of(mime, filename=""):
    e = EXT.get((mime or "").lower())
    if e:
        return e
    if filename and "." in filename:
        e = filename.rsplit(".", 1)[-1].lower()
        if 1 <= len(e) <= 5 and e.isalnum():
            return e
    return "bin"


def dirs(root, chat_id):
    base = os.path.join(root, str(chat_id))
    return {"base": base, "files": os.path.join(base, "files"),
            "authors": os.path.join(base, "by-author"),
            "thumbs": os.path.join(base, "thumbs"), "tmp": os.path.join(base, "tmp"),
            "db": os.path.join(base, "index.db"), "html": os.path.join(base, "index.html")}


def blob_rel(sha, ext):
    return f"files/{sha[:2]}/{sha}.{ext}"


def link_rel(author_dir, ts, msg_id, kind, sha, ext):
    day = time.strftime("%Y%m%d", time.localtime(ts))
    return f"by-author/{author_dir}/{day}_{msg_id}_{kind}_{sha[:8]}.{ext}"


def resume_offset(size_on_disk, total, align=1 << 20):
    """续传起点：向下对齐到 1 MiB。

    Telegram 的 upload.getFile 要求 offset 是 4096 的倍数，对齐到 1 MiB 最省心，
    代价是最多重下 1 MiB。比声明的总长还大说明这半成品坏了，从头来。
    """
    if total and size_on_disk > total:
        return 0
    return size_on_disk // align * align


class Store:
    def __init__(self, root, chat_id):
        self.chat_id = chat_id
        self.d = dirs(root, chat_id)
        self.con = None

    def open(self):
        for k in ("files", "authors", "thumbs", "tmp"):
            os.makedirs(self.d[k], exist_ok=True)
        self.con = sqlite3.connect(self.d["db"])
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.con.commit()
        self._migrate()
        return self

    def _migrate(self):
        """老库补列：CREATE TABLE IF NOT EXISTS 不会给已经存在的表加列。

        只加列、不动任何数据；动手之前先留一份 index.db.bak。
        """
        cols = (("edited", "INTEGER"), ("deleted", "INTEGER"), ("star", "INTEGER"),
                ("next_try", "INTEGER"), ("edit_seen", "INTEGER"),
                ("caption_before", "TEXT"))
        have = {r["name"] for r in self.con.execute("PRAGMA table_info(messages)")}
        add = [(c, t) for c, t in cols if c not in have]
        if add:
            shutil.copy2(self.d["db"], self.d["db"] + ".bak")
            for c, t in add:
                self.con.execute(f"ALTER TABLE messages ADD COLUMN {c} {t}")
            self.con.commit()
        self._drop_versions()
        self._heal_dups()
        # 编辑换媒体会让内容和别的行「撞车」，以前被当成重发标了 dup_of —— 那是各自
        # 不同的原图，不该合并成一张卡。这里每次都清一遍，等于把这条不变式钉死：
        # 被编辑过的行永远不是「重复」。
        self.con.execute("UPDATE messages SET dup_of=NULL WHERE chat_id=? "
                         "AND edited IS NOT NULL AND dup_of IS NOT NULL", (self.chat_id,))
        self.con.commit()
        # 实时路径的迟到编辑误标（那次它漏了 is_new_edit 这道闸）：edit_date 不比 added 晚
        # 的不是新编辑 —— 清掉假标，顺带删除它们那批没文件的历史版本（原版线上早拿不回，
        # 版本条里只剩空占位）。真编辑的 edit_date 一定晚于入库时间，不会误伤。
        self.con.execute(
            "DELETE FROM revisions WHERE chat_id=? AND path IS NULL AND EXISTS ("
            "SELECT 1 FROM messages m WHERE m.chat_id=revisions.chat_id "
            "AND m.msg_id=revisions.msg_id AND m.added IS NOT NULL "
            "AND m.edited IS NOT NULL AND m.added >= m.edited)", (self.chat_id,))
        self.con.execute("UPDATE messages SET edited=NULL, edit_seen=NULL WHERE chat_id=? "
                         "AND edited IS NOT NULL AND added IS NOT NULL "
                         "AND added >= edited", (self.chat_id,))
        self.con.commit()
        if not self.meta("edited_reset"):
            # 以前的「已编辑」是误判出来的（见 watcher.is_new_edit 的说明），旧标又没法事后
            # 分辨真假，一次清干净；往后只有我们显示的内容真变了才会再标上。
            self.con.execute("UPDATE messages SET edited=NULL WHERE chat_id=? "
                             "AND edited IS NOT NULL", (self.chat_id,))
            self.set_meta("edited_reset", "1")

    def _heal_dups(self):
        """把重复指针理干净：先破掉互相指的环，再把「别处已经下好了」的挂上去。

        环是老版本重发逻辑留下的：同一份文件的两条各自把对方当原件。留着的话画廊里这两张卡
        会一起消失（谁都不是原件），所以消息号大的那条让位，早发的那条当原件。
        下面这段是失败/等待的那条：同一个文件在别处下好了，就挂过去别再自己下一遍
        （失败只有「消息已经不在了」一种原因，可同一个文件往往还被重发过）。
        硬链接不照搬 —— 名字里带的是另一条的消息号 —— 留空走 path，文件照样能播。
        """
        self.con.execute(
            "UPDATE messages SET dup_of=NULL WHERE chat_id=? AND dup_of IS NOT NULL "
            "AND msg_id < dup_of AND EXISTS (SELECT 1 FROM messages x "
            "WHERE x.chat_id=messages.chat_id AND x.msg_id=messages.dup_of "
            "AND x.dup_of=messages.msg_id)", (self.chat_id,))
        rows = self.con.execute(
            "SELECT m.msg_id, d.msg_id AS of, d.sha256, d.path, d.thumb, d.size "
            "FROM messages m JOIN messages d ON d.chat_id=m.chat_id "
            "AND d.file_uid=m.file_uid AND d.status='done' "
            "WHERE m.chat_id=? AND m.status IN ('failed','dup') AND m.msg_id<>d.msg_id "
            "AND m.edited IS NULL",           # 编辑行永远不当重复（与 set_done 同一道不变式）
            (self.chat_id,)).fetchall()
        for r in rows:
            self.con.execute(
                "UPDATE messages SET sha256=?,path=?,link=NULL,thumb=?,status='done',note=NULL,"
                "size=?,dup_of=? WHERE chat_id=? AND msg_id=?",
                (r["sha256"], r["path"], r["thumb"], r["size"], r["of"],
                 self.chat_id, r["msg_id"]))
        if rows:
            self.con.commit()

    def _drop_versions(self):
        """早期做过「编辑留两版」，现在只留最新版：清掉历史版本行和它们独占的旧文件。

        和主行共用同一份文件的那些（那种「编辑」其实什么都没改）不会掉文件 ——
        drop_files 按引用计数走，主行还指着就不会删。跑过一次之后表就没了。
        """
        if not self.con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='versions'").fetchone():
            return
        for v in self.con.execute("SELECT * FROM versions").fetchall():
            # 和主行同一份内容的那些（那种「编辑」其实什么都没改），连 by-author 硬链接都是
            # 同一个名字，不能按行清 —— 会把主行那份名字一起删掉。它们本来也没什么可删的。
            if v["sha256"] and self._referenced(v["sha256"], "sha256"):
                continue
            self.drop_files(v)
        self.con.execute("DROP TABLE IF EXISTS versions")
        self.con.commit()

    def close(self):
        if self.con is not None:
            self.con.commit()
            self.con.close()
            self.con = None

    def abs(self, rel):
        return os.path.join(self.d["base"], rel.replace("/", os.sep))

    # --- 游标：整批入库成功后才推进，这是不丢消息的全部保证 ---

    def cursor(self):
        r = self.con.execute("SELECT value FROM meta WHERE key=?",
                             (f"cursor:{self.chat_id}",)).fetchone()
        return int(r["value"]) if r else 0

    def set_cursor(self, msg_id):
        self.con.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"cursor:{self.chat_id}", str(int(msg_id))))
        self.con.commit()

    def meta(self, key, default=None):
        r = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_meta(self, key, value):
        self.con.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        self.con.commit()

    # --- 作者目录：一旦定了就不改，免得同一个人的文件被劈成两个目录 ---

    def author_dir(self, author_id, name):
        author_id = 0 if author_id is None else int(author_id)
        r = self.con.execute("SELECT dir,name FROM authors WHERE chat_id=? AND author_id=?",
                             (self.chat_id, author_id)).fetchone()
        if r is not None:
            if name and r["name"] != name:
                self.con.execute("UPDATE authors SET name=? WHERE chat_id=? AND author_id=?",
                                 (name, self.chat_id, author_id))
                self.con.commit()
            return r["dir"]
        base = f"{author_id}_{safe_name(name)}" if name else str(author_id)
        d, n = base, 2
        while self.con.execute("SELECT 1 FROM authors WHERE chat_id=? AND dir=?",
                               (self.chat_id, d)).fetchone():
            d, n = f"{base}~{n}", n + 1
        os.makedirs(os.path.join(self.d["authors"], d), exist_ok=True)
        self.con.execute("INSERT INTO authors(chat_id,author_id,name,dir,first_seen) "
                         "VALUES(?,?,?,?,?)",
                         (self.chat_id, author_id, name, d, int(time.time())))
        self.con.commit()
        return d

    def set_topic_title(self, topic_id, title):
        if topic_id is None or not title:
            return
        self.con.execute("INSERT INTO topics(chat_id,topic_id,title) VALUES(?,?,?) "
                         "ON CONFLICT(chat_id,topic_id) DO UPDATE SET title=excluded.title",
                         (self.chat_id, topic_id, title))
        self.con.commit()

    # --- 去重查询 ---

    def by_uid(self, file_uid):
        """同一个 Telegram 文件（photo.id / document.id）之前见过吗。

        优先返回已经下好的那条，其次返回排队中的那条 —— 两条都不能再下一个。
        """
        if not file_uid:
            return None
        return self.con.execute(
            "SELECT * FROM messages WHERE chat_id=? AND file_uid=? "
            "AND status IN ('done','pending','dup') "
            "ORDER BY CASE status WHEN 'done' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END "
            "LIMIT 1", (self.chat_id, file_uid)).fetchone()

    def get(self, msg_id):
        return self.con.execute("SELECT * FROM messages WHERE chat_id=? AND msg_id=?",
                                (self.chat_id, msg_id)).fetchone()

    def by_sha(self, sha):
        """同样的字节之前存过吗（换个账号重传的文件，file_uid 会变，sha 不变）。"""
        return self.con.execute(
            "SELECT * FROM messages WHERE chat_id=? AND sha256=? AND status='done' LIMIT 1",
            (self.chat_id, sha)).fetchone()

    # --- 写 ---

    def insert(self, row):
        """返回 True 表示确实是新插进去的（INSERT OR IGNORE 撞主键时返回 False）。

        兜底扫描和监听会重复看到同一条消息，靠这个返回值避免重复抓缩略图。
        """
        cols = ",".join(row)
        cur = self.con.execute(
            f"INSERT OR IGNORE INTO messages({cols}) "
            f"VALUES({','.join('?' * len(row))})", tuple(row.values()))
        return cur.rowcount > 0

    def dups_of(self, msg_id):
        """入库时被挂到这条上的重复行（同一个文件重发）。"""
        return self.con.execute(
            "SELECT * FROM messages WHERE chat_id=? AND dup_of=?",
            (self.chat_id, msg_id)).fetchall()

    def set_link(self, msg_id, link):
        self.con.execute("UPDATE messages SET link=? WHERE chat_id=? AND msg_id=?",
                         (link, self.chat_id, msg_id))
        self.con.commit()

    def commit(self):
        self.con.commit()

    def mark_deleted(self, msg_id, when=None):
        """Telegram 里那条被删了。只打标记 —— 本地文件留不留由网页上的删除按钮决定。"""
        self.con.execute(
            "UPDATE messages SET deleted=? WHERE chat_id=? AND msg_id=? "
            "AND COALESCE(deleted,0)=0",
            (int(when or time.time()), self.chat_id, msg_id))
        self.con.commit()

    def set_status(self, msg_id, status, note=None, **kw):
        sets, vals = ["status=?", "note=?"], [status, note]
        for k, v in kw.items():
            sets.append(f"{k}=?")
            vals.append(v)
        vals += [self.chat_id, msg_id]
        self.con.execute(f"UPDATE messages SET {','.join(sets)} "
                         f"WHERE chat_id=? AND msg_id=?", vals)

    def set_done(self, msg_id, sha, path, link, thumb, dup_of=None):
        """下好了。顺手把等着这条的 dup 行补齐 —— 它们指向同一份文件。

        dup_of 用在「内容一样但重新上传过」的情况：file_uid 变了、sha 没变，挂到
        老文件那条上，画廊里就不会同一份东西显示两张卡。

        换过媒体的那条（编辑）：新版在这里落地，旧文件到这一刻才没人要 —— 之前一直留着，
        卡片还能点开看/播。所以清旧文件放在这里，而不是编辑发生的时候。
        """
        old = self.get(msg_id)
        self.con.execute(
            "UPDATE messages SET sha256=?,path=?,link=?,thumb=?,status='done',note=NULL "
            "WHERE chat_id=? AND msg_id=?", (sha, path, link, thumb, self.chat_id, msg_id))
        self.con.execute(
            "UPDATE messages SET sha256=?,path=?,link=?,thumb=?,status='done',note=NULL "
            "WHERE chat_id=? AND dup_of=?", (sha, path, link, thumb, self.chat_id, msg_id))
        # 同一个文件之前下失败/挂成 dup 的那条：这次成功了，挂到这条上别再自己下一遍。
        # 编辑行排除（永远不当重复，同 set_done 主体的不变式）；size 顺手对齐到这份真身的
        # 实际大小 —— 之前各自记的是打进来的 Telegram 元数据，渐进式照片会和真身差一点。
        self.con.execute(
            "UPDATE messages SET sha256=?,path=?,link=?,thumb=?,status='done',note=NULL,"
            "size=(SELECT s.size FROM messages s WHERE s.chat_id=? AND s.msg_id=?),"
            "dup_of=? WHERE chat_id=? AND msg_id<>? AND status IN ('failed','dup') "
            "AND edited IS NULL "
            "AND file_uid=(SELECT file_uid FROM messages WHERE chat_id=? AND msg_id=?)",
            (sha, path, link, thumb, self.chat_id, msg_id, msg_id,
             self.chat_id, msg_id, self.chat_id, msg_id))
        if dup_of is not None and not (old and old["edited"]):
            # 被编辑过的行永远不是「重复」：编辑换上去的是**另一个**文件，只是内容撞车
            # （几条不同的图被作者改成同一张）。这条不变式在 watcher 和 _migrate 里也各有一道。
            # 对方已经把我当原件了就别再指回去：同一份文件的两条互相指会绕成环，
            # 画廊里两张卡会一起消失（_dup_root 虽然兜得住，但数据本身该是干净的）
            self.con.execute(
                "UPDATE messages SET dup_of=? WHERE chat_id=? AND msg_id=? "
                "AND COALESCE((SELECT x.dup_of FROM messages x WHERE x.chat_id=messages.chat_id "
                "AND x.msg_id=?),0)<>msg_id",
                (dup_of, self.chat_id, msg_id, dup_of))
        if old is not None and (old["path"] != path or old["link"] != link
                                or old["thumb"] != thumb):
            self.drop_files(old)          # 换媒体之前那份，到现在才真没人要
        self.con.commit()

    # --- 编辑历史 ---

    def add_revision(self, msg_id, edited_at=None):
        """把这一行**当前**的媒体存成一条历史版本（编辑发生之前的那一刻）。

        多次编辑就是多条，seq 递增。存的是同一份内容寻址文件（不复制、不占额外磁盘），
        「编辑不删内容」靠的是 drop_files 的引用计数把 revisions 也算进来。
        """
        r = self.get(msg_id)
        if r is None or not (r["path"] or r["thumb"]):
            return None
        n = self.con.execute(
            "SELECT COALESCE(MAX(seq),0)+1 s FROM revisions WHERE chat_id=? AND msg_id=?",
            (self.chat_id, msg_id)).fetchone()["s"]
        self.con.execute(
            "INSERT INTO revisions (chat_id,msg_id,seq,kind,size,mime,width,height,duration,"
            "caption,sha256,path,thumb,edited_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.chat_id, msg_id, n, r["kind"], r["size"], r["mime"], r["width"], r["height"],
             r["duration"], r["caption"], r["sha256"], r["path"], r["thumb"], edited_at))
        self.con.commit()
        return n

    def revisions(self, msg_id):
        return self.con.execute(
            "SELECT * FROM revisions WHERE chat_id=? AND msg_id=? ORDER BY seq",
            (self.chat_id, msg_id)).fetchall()

    def drop_revisions(self, msg_id):
        """删这条的全部历史版本（索引 + 文件）。只有用户主动删卡片才走这里。"""
        rows = self.revisions(msg_id)
        self.con.execute("DELETE FROM revisions WHERE chat_id=? AND msg_id=?",
                         (self.chat_id, msg_id))
        self.con.commit()
        return sum(self.drop_files({"link": None, "file_uid": None, "sha256": r["sha256"],
                                    "path": r["path"], "size": r["size"],
                                    "thumb": r["thumb"]}) for r in rows)

    def drop_revision(self, msg_id, seq):
        """只删这一版历史（原图/第 N 次编辑前）。先删索引行，再用 drop_files 的
        引用计数落文件 —— 这个 sha 还有别的行/版本指着时文件不删，只丢索引。"""
        r = self.con.execute(
            "SELECT * FROM revisions WHERE chat_id=? AND msg_id=? AND seq=?",
            (self.chat_id, msg_id, seq)).fetchone()
        if r is None:
            return None
        self.con.execute("DELETE FROM revisions WHERE chat_id=? AND msg_id=? AND seq=?",
                         (self.chat_id, msg_id, seq))
        self.con.commit()
        return self.drop_files({"link": None, "file_uid": None, "sha256": r["sha256"],
                                "path": r["path"], "size": r["size"], "thumb": r["thumb"]})

    def drop_current(self, msg_id):
        """删「现在」这一版：不是删整条，而是就地回退一版。

        参数里最新那条历史（seq 最大）顶上成为新的「现在」—— 卡片照样有大图，时间线少一格。
        旧的那份文件到这一刻才没人要（引用计数还看着别的行/版本，别处还在用就不删盘）。
        历史已经空了（这条只剩这一份文件）时，没有东西可以顶上来，那就整条删掉 ——
        留着一条没文件、没历史的壳没有意义。
        """
        row = self.get(msg_id)
        if row is None:
            return None
        last = self.con.execute(
            "SELECT * FROM revisions WHERE chat_id=? AND msg_id=? ORDER BY seq DESC LIMIT 1",
            (self.chat_id, msg_id)).fetchone()
        if last is None:
            self.delete([msg_id])
            return {"deleted": 1, "bytes": 0}
        old = row
        # 顺序要紧：drop_files 是按「现在还有谁指着这个 sha」判的，所以必须先把消息行
        # 换成新「现在」，再释放旧那份 —— 反过来的话它自己就还指着老 sha，永远判成「有人要」。
        # link（作者目录陈列名）也必须一起清：revisions 里没存 link，留着就会把**旧版的**
        # 名字（连扩展名都是旧媒体的）挂在新的内容上 —— 网页接口 link 优先，媒体直接放错。
        self.con.execute(
            "DELETE FROM revisions WHERE chat_id=? AND msg_id=? AND seq=?",
            (self.chat_id, msg_id, last["seq"]))
        self.con.execute(
            "UPDATE messages SET kind=?,size=?,mime=?,width=?,height=?,duration=?,caption=?,"
            "sha256=?,path=?,link=NULL,thumb=? WHERE chat_id=? AND msg_id=?",
            (last["kind"], last["size"], last["mime"], last["width"], last["height"],
             last["duration"], last["caption"], last["sha256"], last["path"], last["thumb"],
             self.chat_id, msg_id))
        self.con.commit()
        freed = self.drop_files(old)          # 旧「现在」那份：没别的行/版本要了才真删
        return {"deleted": 0, "bytes": freed}

    # --- 读 ---

    def pending(self, need="full", limit=1000, now=None):
        """need='thumb' 只挑还没缩略图的，need='full' 挑还没下完的。

        next_try 是失败后的退避时间：没到点的不再挑，免得一次网络抖动就把重试次数烧光。
        """
        cond = "status='pending'" if need == "full" else "status='pending' AND thumb IS NULL"
        vals = [self.chat_id]
        if need == "full":
            cond += " AND (next_try IS NULL OR next_try <= ?)"
            vals.append(int(now or time.time()))
        vals.append(limit)
        return self.con.execute(
            f"SELECT m.*, a.name AS author_name FROM messages m "
            f"LEFT JOIN authors a ON a.chat_id=m.chat_id AND a.author_id=m.author_id "
            f"WHERE m.chat_id=? AND {cond} ORDER BY m.date LIMIT ?", vals).fetchall()

    def counts(self):
        rows = self.con.execute(
            "SELECT status, COUNT(*) n, COALESCE(SUM(size),0) b FROM messages "
            "WHERE chat_id=? GROUP BY status", (self.chat_id,)).fetchall()
        return {r["status"]: (r["n"], r["b"]) for r in rows}

    def _items_where(self, kinds=None, status=None, author=None, topic=None):
        """列表和导出共用的一套筛选，保证「导出的就是屏幕上筛出来的那些」。"""
        where, vals = ["m.chat_id=?"], [self.chat_id]
        if kinds:
            where.append(f"m.kind IN ({','.join('?' * len(kinds))})")
            vals += list(kinds)
        if author is not None:
            where.append("m.author_id = ?")
            vals.append(int(author))
        if topic is not None:
            where.append("m.topic_id = ?")
            vals.append(int(topic))
        if status == "deleted":                  # 伪状态：Telegram 上被删掉的那些
            where.append("m.deleted IS NOT NULL")
        elif status == "star":                   # 伪状态：收藏的
            where.append("COALESCE(m.star,0)=1")
        elif status == "edited":                 # 伪状态：编辑过媒体的
            where.append("m.edited IS NOT NULL")
        elif status:
            where.append("m.status = ?")
            vals.append(status)
        return where, vals

    def count(self, kinds=None, status=None, author=None, topic=None):
        """当前筛选下一共多少条。顶部的「共 N 条」和导出提示都用它，口径跟 items 一样。"""
        where, vals = self._items_where(kinds, status, author, topic)
        return self.con.execute(
            f"SELECT COUNT(*) n FROM messages m WHERE {' AND '.join(where)}",
            vals).fetchone()["n"]

    def items(self, after_msg=None, before_msg=None, limit=200, kinds=None, status=None,
              author=None, topic=None):
        """条目查询。after_msg 给实时页用（只取更新的），before_msg 给翻页用。

        排序键用 msg_id 不用 rowid：rowid 是入库顺序，补历史/兜底扫描后入库的老消息
        会跑到上面去。msg_id 在同一个群里随时间是递增的，才是真正的先后顺序。
        """
        where, vals = self._items_where(kinds, status, author, topic)
        if after_msg is not None:
            where.append("m.msg_id > ?")
            vals.append(after_msg)
        if before_msg is not None:
            where.append("m.msg_id < ?")
            vals.append(before_msg)
        vals.append(limit)
        return self.con.execute(
            "SELECT m.*, a.name AS author_name, t.title AS topic_title "
            "FROM messages m "
            "LEFT JOIN authors a ON a.chat_id=m.chat_id AND a.author_id=m.author_id "
            "LEFT JOIN topics t ON t.chat_id=m.chat_id AND t.topic_id=m.topic_id "
            f"WHERE {' AND '.join(where)} ORDER BY m.msg_id DESC LIMIT ?", vals).fetchall()

    def used_bytes(self):
        """库里真实内容占多少。同样的字节只算一次（按 sha 分组）。
        缩略图和 tmp 里的半成品不计（相对内容量可忽略）。编辑留下的历史版本算进来。"""
        r = self.con.execute(
            "SELECT COALESCE(SUM(size),0) b FROM (SELECT MAX(size) size FROM ("
            "SELECT sha256, size FROM messages WHERE chat_id=? AND status='done' "
            "AND sha256 IS NOT NULL UNION ALL "
            "SELECT sha256, size FROM revisions WHERE chat_id=? AND sha256 IS NOT NULL"
            ") GROUP BY sha256)", (self.chat_id, self.chat_id)).fetchone()
        return r["b"] or 0

    def authors(self, limit=500):
        """作者列表：条数 + 占用（占用按 sha 去重，跟 used_bytes 一个口径）。

        全删光的作者也列出来（n=0、占 0）：筛选器要停在他那一档，删空后不弹回「全部」。
        **按名称升序**（没名字用 id）：名字不随删改而变 → 下拉顺序永不漂移，
        滚轮翻作者时前后邻居始终稳定（按占用排的话每删一条全家都要挪位）。
        """
        n = {r["author_id"]: r["n"] for r in self.con.execute(
            "SELECT author_id, COUNT(*) n FROM messages WHERE chat_id=? "
            "GROUP BY author_id", (self.chat_id,)).fetchall()}
        used = {r["author_id"]: r["b"] for r in self.con.execute(
            "SELECT author_id, SUM(sz) b FROM (SELECT author_id, MAX(size) sz FROM messages "
            "WHERE chat_id=? AND status='done' AND sha256 IS NOT NULL "
            "GROUP BY author_id, sha256) GROUP BY author_id", (self.chat_id,)).fetchall()}
        names = {r["author_id"]: r["name"] for r in self.con.execute(
            "SELECT author_id, name FROM authors WHERE chat_id=?", (self.chat_id,)).fetchall()}
        out = [{"author_id": k, "name": names.get(k) or "", "n": v, "bytes": used.get(k, 0)}
               for k, v in n.items()]
        out += [{"author_id": k, "name": nm or "", "n": 0, "bytes": 0}
                for k, nm in names.items() if k not in n]
        out.sort(key=lambda x: (x["name"] or "", str(x["author_id"] or "")))
        return out[:limit]

    def _referenced(self, value, col, table="messages"):
        """这个 sha / 缩略图还有没有别的行（或历史版本）指着。"""
        return bool(self.con.execute(
            f"SELECT 1 FROM {table} WHERE chat_id=? AND {col}=? LIMIT 1",
            (self.chat_id, value)).fetchone())

    def drop_files(self, r):
        """删这一行占的文件。硬链接、缩略图、半成品直接删；
        真身文件只在该 sha 再没人引用时才删。返回真正释放的字节数。

        缩略图按 thumb 这一列判引用（不是按 file_uid）：入库时生成的重复行，
        thumb 存的就是原件的图，按 file_uid 判会把原件的缩略图一起删掉。
        """
        freed = 0
        if r["link"]:
            _unlink(self.abs(r["link"]))
        if r["file_uid"]:
            _unlink(self.abs(f"tmp/{r['file_uid']}.part"))
        # 历史版本（revisions）也指着同一份文件/缩略图时不能删 —— 「编辑不删内容」就在这一行
        if r["sha256"] and r["path"] and not self._referenced(r["sha256"], "sha256") \
                and not self._referenced(r["sha256"], "sha256", "revisions"):
            if _unlink(self.abs(r["path"])):
                freed += r["size"] or 0
        if r["thumb"] and not self._referenced(r["thumb"], "thumb") \
                and not self._referenced(r["thumb"], "thumb", "revisions"):
            _unlink(self.abs(r["thumb"]))
        return freed

    def _orphan_dups(self, gone):
        """删掉的行原来是别人的「原件」：把还活着的重复行推一条当新原件。

        不做的话它们的 dup_of 指向已经不存在的行，_dup_root 找不到原件，
        就会永远停在「跟另一条重复，等它下完」那个状态上。
        """
        if not gone:
            return
        q = ",".join("?" * len(gone))
        kids = self.con.execute(
            f"SELECT msg_id, path FROM messages WHERE chat_id=? AND dup_of IN ({q}) "
            "ORDER BY msg_id", (self.chat_id, *gone)).fetchall()
        if not kids:
            return
        # 有文件的当新原件；都没有（原件当时还没下完）就交给下载队列去下
        root = next((k for k in kids if k["path"]), kids[0])
        self.con.execute(
            "UPDATE messages SET dup_of=NULL, status=?, note=NULL "
            "WHERE chat_id=? AND msg_id=?",
            ("done" if root["path"] else "pending", self.chat_id, root["msg_id"]))
        self.con.execute(
            f"UPDATE messages SET dup_of=? WHERE chat_id=? AND dup_of IN ({q})",
            (root["msg_id"], self.chat_id, *gone))
        self.con.commit()

    def retry_one(self, msg_id):
        """手动重试：把「下载失败」的行放回队列。

        行本身、缩略图、历史版本一概不动 —— 缩略图还在被行引用着，失败只是下载状态，
        不是数据出问题。消息真的被删了（MEDIA_GONE）走的是 skipped，到不了这里。
        """
        row = self.get(msg_id)
        if row is None:
            return {"error": "这条不在库里"}
        if row["status"] != "failed":
            return {"error": "只有「下载失败」的行能重试"}
        self.set_status(msg_id, "pending", None, tries=0, next_try=0)
        self.con.commit()
        return {}

    def delete(self, msg_ids):
        """整条删：索引 + 文件。

        内容寻址的文件可能被多条共用（dup 就是这种情况），所以先删行、再按引用数清理。
        """
        rows = [r for r in (self.get(int(i)) for i in msg_ids) if r is not None]
        if not rows:
            return {"deleted": 0, "bytes": 0}
        for r in rows:
            self.con.execute("DELETE FROM messages WHERE chat_id=? AND msg_id=?",
                             (self.chat_id, r["msg_id"]))
        self.con.commit()
        self._orphan_dups([r["msg_id"] for r in rows])
        # 历史版本先删（否则引用计数会以为旧文件还有人要），再按引用数清文件
        freed = sum(self.drop_revisions(r["msg_id"]) for r in rows)
        freed += sum(self.drop_files(r) for r in rows)
        self.commit()
        return {"deleted": len(rows), "bytes": freed}

    def delete_status(self, status):
        """按状态整批删（清理「跳过」用）。"""
        ids = [r["msg_id"] for r in self.con.execute(
            "SELECT msg_id FROM messages WHERE chat_id=? AND status=?",
            (self.chat_id, status)).fetchall()]
        return self.delete(ids)

    def delete_deleted(self):
        """整批清理「Telegram 上已经被删掉」的条目（连文件）。"""
        ids = [r["msg_id"] for r in self.con.execute(
            "SELECT msg_id FROM messages WHERE chat_id=? AND deleted IS NOT NULL",
            (self.chat_id,)).fetchall()]
        return self.delete(ids)

    def topics(self, limit=500):
        """话题：标题 + 条数 + 占用。全删光的也列（n=0）；**按标题升序**（没标题用 id），
        同 authors() —— 名字不随删改而变，排序永不漂。"""
        rows = self.con.execute(
            "SELECT m.topic_id, COUNT(*) n, COALESCE(SUM(m.size),0) b FROM messages m "
            "WHERE m.chat_id=? AND m.topic_id IS NOT NULL GROUP BY m.topic_id",
            (self.chat_id,)).fetchall()
        titles = {r["topic_id"]: r["title"] for r in self.con.execute(
            "SELECT topic_id, title FROM topics WHERE chat_id=?", (self.chat_id,)).fetchall()}
        out = [{"topic_id": r["topic_id"], "title": titles.get(r["topic_id"]) or "",
                "n": r["n"], "bytes": r["b"]} for r in rows]
        out += [{"topic_id": tid, "title": ttl or "", "n": 0, "bytes": 0}
                for tid, ttl in titles.items()
                if tid not in {r["topic_id"] for r in rows}]
        out.sort(key=lambda x: (x["title"] or "", str(x["topic_id"] or "")))
        return out[:limit]

    def top(self, limit=200):
        """最大的文件排行（只算真下了的）。历史版本不列进来，但占用统计里算。"""
        return self.con.execute(
            "SELECT m.msg_id, m.kind, m.size, m.caption, m.date, m.thumb, m.grouped_id, "
            "m.author_id, a.name AS author_name FROM messages m "
            "LEFT JOIN authors a ON a.chat_id=m.chat_id AND a.author_id=m.author_id "
            "WHERE m.chat_id=? AND m.status='done' AND m.sha256 IS NOT NULL "
            "ORDER BY m.size DESC LIMIT ?", (self.chat_id, limit)).fetchall()

    def set_star(self, msg_id, on):
        self.con.execute("UPDATE messages SET star=? WHERE chat_id=? AND msg_id=?",
                         (1 if on else 0, self.chat_id, msg_id))
        self.commit()
        return bool(on)

    def gallery_rows(self, limit=5000):
        """静态导出用。全量导出会生成一个巨大的 HTML，所以只导最近的（按消息号）。"""
        return self.con.execute(
            "SELECT m.*, a.name AS author_name, t.title AS topic_title "
            "FROM messages m "
            "LEFT JOIN authors a ON a.chat_id=m.chat_id AND a.author_id=m.author_id "
            "LEFT JOIN topics t ON t.chat_id=m.chat_id AND t.topic_id=m.topic_id "
            "WHERE m.chat_id=? ORDER BY m.msg_id DESC LIMIT ?",
            (self.chat_id, limit)).fetchall()


def chat_dirs(root):
    """library\\ 下已经存在的群目录（启动时按这个把各群的库打开）。"""
    if not os.path.isdir(root):
        return []
    out = []
    for name in os.listdir(root):
        if name.startswith("-") and os.path.isdir(os.path.join(root, name)):
            try:
                out.append(int(name))
            except ValueError:
                pass
    return out


class Config:
    """本地小配置：api_id / api_hash。环境变量优先，其次 library\\index.db 的 settings 表。

    api_hash 和 session 都是凭证：落盘的只有密文（secure.py 的 DPAPI + 可选口令）。

    两个值的地位不一样，别混：
      api_id   每次连接都要发（initConnection 的必填字段，telegrambaseclient.py:368）
      api_hash 只在**登录**时才用得上（auth.sendCode / exportLoginToken）—— session
               还活着时它一次都不发，所以可以留空，真需要重新登录了再要真的。
    """

    PLACEHOLDER_HASH = "0" * 32

    def __init__(self, root, sealer=None):
        self.reg = Registry(root).open()
        self.sealer = sealer or secure.Sealer()

    def _get(self, key, env):
        if env:
            v = os.environ.get(env)
            if v:
                return v
        return self.reg.setting(key)

    @property
    def api_id(self):
        try:
            v = self._get("api_id", "TG_API_ID")
            return int(v) if v else None
        except (TypeError, ValueError):
            return None

    @property
    def api_hash_set(self):
        """填过真的没有。网页上只报这个，不回明文 —— 所以这里只看密文在不在。"""
        if os.environ.get("TG_API_HASH"):
            return True
        return bool(self.reg.setting("api_hash_enc", ""))

    @property
    def api_hash(self):
        env = os.environ.get("TG_API_HASH")
        if env:
            return env.strip()
        enc = self.reg.setting("api_hash_enc", "")
        if not enc:
            return self.PLACEHOLDER_HASH
        return self.sealer.unseal(secure.b64d(enc)).decode("utf-8")

    @property
    def ready(self):
        return bool(self.api_id)

    def save(self, api_id=None, api_hash=None):
        if api_id:
            self.reg.set_setting("api_id", int(api_id))
        if api_hash:
            h = str(api_hash).strip()
            self.reg.set_setting("api_hash_enc",
                                 secure.b64e(self.sealer.seal(h.encode("utf-8"))))
            self.reg.del_setting("api_hash")            # 老版本留下的明文，别留着

    def migrate_plaintext(self, sealer):
        """老版本把 api_hash 明文写在 settings 里：就地加密、删掉明文。"""
        self.sealer = sealer
        v = self.reg.setting("api_hash", "")
        if v:
            self.save(api_hash=v)

    def reseal(self, sealer):
        """换密封器（设/清口令）：把库里那份 api_hash 按新密封器重写一遍。

        读的时候两个密封器都试：重启后 self.sealer 还是「没口令」那个，而库里那份可能是
        带口令封的；设口令时正好反过来。都解不开（换了 Windows 账户）就原样留着，
        等重新登录时重填 —— 这里不猜、也不覆盖。
        """
        enc = self.reg.setting("api_hash_enc", "")
        old = self.sealer
        self.sealer = sealer
        if not enc:
            return
        v = None
        for s in (old, sealer):
            try:
                v = s.unseal(secure.b64d(enc))
                break
            except OSError:
                continue
        if v is not None:
            self.save(api_hash=v.decode("utf-8"))


class Registry:
    """全局小库：监视配置和总设置。媒体索引在各群自己的库里。"""

    def __init__(self, root):
        self.root = root
        self.path = os.path.join(root, "index.db")
        self.con = None

    def open(self):
        os.makedirs(self.root, exist_ok=True)
        self.con = sqlite3.connect(self.path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(REGISTRY_SCHEMA)
        # 老库的 watches 没有 authors 列（作者白名单，2026-10 加的）
        cols = {r["name"] for r in
                self.con.execute("PRAGMA table_info(watches)").fetchall()}
        if "authors" not in cols:
            self.con.execute("ALTER TABLE watches ADD COLUMN authors TEXT "
                             "NOT NULL DEFAULT ''")
        self.con.commit()
        return self

    def close(self):
        if self.con is not None:
            self.con.commit()
            self.con.close()
            self.con = None

    def watch(self, chat_id):
        return self.con.execute("SELECT * FROM watches WHERE chat_id=?", (chat_id,)).fetchone()

    def watches(self):
        return self.con.execute("SELECT * FROM watches ORDER BY added_at").fetchall()

    def enabled(self):
        return self.con.execute("SELECT * FROM watches WHERE enabled=1").fetchall()

    def set_watch(self, chat_id, title=None, kinds=None, max_mb=None, enabled=None,
                  authors=None):
        if self.watch(chat_id) is None:
            self.con.execute(
                "INSERT INTO watches(chat_id,title,kinds,authors,max_mb,enabled,"
                "added_at,started_at) VALUES(?,?,?,?,?,?,?,?)",
                (chat_id, title, kinds or "photo,video", authors or "",
                 max_mb or 100, 1 if enabled else 0, int(time.time()),
                 int(time.time()) if enabled else None))
        else:
            sets, vals = [], []
            if title is not None:
                sets.append("title=?")
                vals.append(title)
            if kinds is not None:
                sets.append("kinds=?")
                vals.append(kinds)
            if authors is not None:
                sets.append("authors=?")
                vals.append(authors)
            if max_mb is not None:
                sets.append("max_mb=?")
                vals.append(max_mb)
            if enabled is not None:
                sets.append("enabled=?")
                vals.append(1 if enabled else 0)
                sets.append("started_at=?")
                vals.append(int(time.time()) if enabled else None)
            if sets:
                vals.append(chat_id)
                self.con.execute(f"UPDATE watches SET {','.join(sets)} WHERE chat_id=?", vals)
        self.con.commit()
        return self.watch(chat_id)

    def setting(self, key, default=None):
        r = self.con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key, value):
        self.con.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, str(value)))
        self.con.commit()

    def del_setting(self, key):
        self.con.execute("DELETE FROM settings WHERE key=?", (key,))
        self.con.commit()

    def disk_cap(self):
        """磁盘占用上限（字节）。默认 50 GB。"""
        try:
            gb = float(self.setting("disk_cap_gb", 50))
        except (TypeError, ValueError):
            gb = 50
        return int(gb * (1 << 30))
