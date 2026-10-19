"""条目聚合 + 静态离线导出。

库里是一条消息一行（每条有自己的永久链接、作者、话题），显示的时候才按
grouped_id 把相册聚成一张卡。group_items() 同时给网页端和这里的静态导出用。

静态导出是为了「关掉程序也能看」：双击 index.html，图片直接显示、视频用
<video> 播，全是相对路径，不依赖服务。功能追着在线页走（但只读）：
瀑布流、搜索/类型/状态/作者/话题筛选、可点的作者/话题胶囊、滚轮切下拉、
跨卡翻页的灯箱（←→/Esc/滚轮）、编辑历史展开条。
"""

import html
import os
import time
from urllib.parse import quote

CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#14161a;color:#e6e6e6;
     font:14px/1.5 "Segoe UI","Microsoft YaHei",sans-serif}
header{position:sticky;top:0;z-index:9;background:#1b1e24;border-bottom:1px solid #2c313a;
       padding:12px 16px}
h1{margin:0 0 6px;font-size:16px}
.stats{color:#9aa3b2;font-size:13px}
.bar{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;align-items:center}
input[type=search],select{background:#0f1115;color:#e6e6e6;border:1px solid #2c313a;
       border-radius:6px;padding:6px 9px;font:inherit;min-width:160px}
#n{color:#9aa3b2}
/* 瀑布流：行高 1px，每张卡用 JS 实测高度算 span，和在线页同款 */
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px;
     grid-auto-rows:1px;padding:16px}
figure{margin:0;background:#1b1e24;border:1px solid #2c313a;border-radius:10px;overflow:hidden;
       display:flex;flex-direction:column}
.grid{display:grid;gap:2px;background:#0f1115}
.grid.n1{grid-template-columns:1fr}
.grid.n2{grid-template-columns:1fr 1fr}
.grid.n3,.grid.n4{grid-template-columns:1fr 1fr}
.grid .mb{display:block;position:relative;aspect-ratio:1/1;overflow:hidden;background:#0f1115}
.grid img,.grid video{width:100%;height:100%;object-fit:cover;display:block}
.play{position:absolute;right:6px;bottom:6px;background:#0009;border-radius:4px;
      padding:1px 6px;font-size:12px}
.none{padding:18px 10px;text-align:center;color:#6b7280;font-size:12px}
figcaption{padding:9px 11px;display:flex;flex-direction:column;gap:4px}
.cap{font-size:13px;max-height:3.2em;overflow:hidden}
.cap.full{max-height:none;cursor:auto}
.meta{color:#9aa3b2;font-size:12px;word-break:break-all}
.meta a{color:#6cb6ff}
.k{display:inline-block;padding:0 6px;border-radius:4px;background:#2c313a;font-size:11px}
.au{color:#6cb6ff;cursor:pointer}
.au:hover{text-decoration:underline}
.bad{color:#e0a05a}
.alb{color:#6cb6ff}
/* 「已编辑」展开里的版本条：原图 → 每次编辑前 → 现在，每张点开就是那一版的文件。
   编辑历史归属活着的成员（已删除成员的历史不算到留守卡头上）。 */
.revs{margin-top:2px}
.revs summary{cursor:pointer;color:#9aa3b2;font-size:12px}
.revstrip{display:flex;gap:8px;overflow-x:auto;padding-top:6px}
.revcell{flex:0 0 auto;width:88px;text-align:center;font-size:11px;color:#9aa3b2}
.revcell a{display:block;position:relative;aspect-ratio:1/1;overflow:hidden;border-radius:6px;
           background:#0f1115}
.revcell img{width:100%;height:100%;object-fit:cover;display:block}
.revcap{padding-top:3px}
/* 灯箱：←→ 跨卡翻页、Esc/点背景关、滚轮翻页；离线只读，不给删除/分享 */
#lbox{display:none;position:fixed;inset:0;background:#000d;z-index:99;
      align-items:center;justify-content:center}
#lbox.on{display:flex}
#lbmid img,#lbmid video{max-width:92vw;max-height:92vh;border-radius:10px;background:#0f1115}
#lbinfo{position:fixed;top:10px;left:50%;transform:translateX(-50%);background:#0009;
        color:#fff;border-radius:14px;padding:4px 12px;font-size:13px}
"""

JS = """
const q=document.getElementById('q'),k=document.getElementById('k'),
      s=document.getElementById('s'),au=document.getElementById('au'),
      tp=document.getElementById('tp'),n=document.getElementById('n');
const cards=[...document.querySelectorAll('figure')];
function hit(f){
  const t=q.value.trim().toLowerCase();
  if(t && !f.dataset.blob.includes(t))return false;
  if(k.value && !f.dataset.k.split(',').includes(k.value))return false;
  const ss=s.value;                                   // 状态/标记类筛选
  if(ss){
    if(ss==='deleted'){if(f.dataset.deleted!=='1')return false;}
    else if(ss==='edited'){if(f.dataset.edited!=='1')return false;}
    else if(ss==='star'){if(f.dataset.star!=='1')return false;}
    else if(f.dataset.status!==ss)return false;
  }
  if(au.value && f.dataset.au!==au.value)return false;
  if(tp.value && f.dataset.tp!==tp.value)return false;
  return true;
}
function apply(){
  let c=0;
  for(const f of cards){
    const ok=hit(f);
    f.style.display=ok?'':'none';
    if(ok)c++;
  }
  n.textContent=c+' / '+cards.length;
  fit();
}
q.addEventListener('input',apply);
/* 滚轮直接切下拉选项（在线页同款） */
for(const sel of [k,s,au,tp])sel.addEventListener('wheel',e=>{
  e.preventDefault();
  if(sel.options.length<2)return;
  sel.selectedIndex=(sel.selectedIndex+(e.deltaY>0?1:-1)+sel.options.length)%sel.options.length;
  apply();
},{passive:false});
/* 卡片上的作者/话题胶囊点一下 = 选中筛选 */
for(const el of document.querySelectorAll('[data-au]'))el.onclick=()=>{au.value=el.dataset.au;apply();};
for(const el of document.querySelectorAll('[data-tp]'))el.onclick=()=>{tp.value=el.dataset.tp;apply();};
/* 超出 3 行的说明点开看全文 */
for(const cap of document.querySelectorAll('.cap'))
  cap.onclick=()=>{if(cap.scrollHeight>cap.clientHeight+2||cap.classList.contains('full'))
    cap.classList.toggle('full');};
/* 瀑布流：每卡实测高度算 span；懒加载图 onload 后重排 */
function fit(){
  const gap=parseFloat(getComputedStyle(document.querySelector('main')).rowGap)||14;
  for(const f of cards){
    if(f.style.display==='none')continue;
    const h=f.getBoundingClientRect().height;
    if(!h)continue;
    f.style.gridRowEnd='span '+Math.ceil((h+gap)/(1+gap));
  }
}
for(const img of document.querySelectorAll('.grid img'))img.onload=fit;
addEventListener('resize',fit);
/* 灯箱：点 .mb 打开，列表 = 当前可见卡片的全部媒体（跨卡翻页） */
let lb=null;
const overlay=document.createElement('div');overlay.id='lbox';
overlay.innerHTML='<div id="lbmid"></div><div id="lbinfo"></div>';
document.body.appendChild(overlay);
document.getElementById('lbmid').onclick=e=>e.stopPropagation();  // 点媒体不关灯箱
function lbList(){
  const out=[];
  for(const f of cards){
    if(f.style.display==='none')continue;
    out.push(...f.querySelectorAll('.mb'));
  }
  return out;
}
function lbDraw(){
  const a=lb.list[lb.i],mid=document.getElementById('lbmid'),
        info=document.getElementById('lbinfo');
  mid.innerHTML='';
  if(a.dataset.kind==='video'){
    const v=document.createElement('video');
    v.controls=true;v.autoplay=true;v.src=a.dataset.file;
    if(a.dataset.thumb)v.poster=a.dataset.thumb;
    mid.appendChild(v);
  }else{
    const im=document.createElement('img');im.src=a.dataset.file;mid.appendChild(im);
  }
  info.textContent=(lb.i+1)+' / '+lb.list.length+(a.dataset.kind==='video'?' · 视频':'');
}
function lbOpen(a){
  lb={list:lbList(),i:Math.max(0,lb.list.indexOf(a))};
  lbDraw();overlay.classList.add('on');
}
function lbClose(){
  lb=null;overlay.classList.remove('on');
  document.getElementById('lbmid').innerHTML='';
}
function lbStep(d){
  if(!lb||!lb.list.length)return;
  lb.i=(lb.i+d+lb.list.length)%lb.list.length;
  lbDraw();
}
for(const a of document.querySelectorAll('.mb'))
  a.onclick=e=>{e.preventDefault();lbOpen(a);};
overlay.onclick=lbClose;
overlay.addEventListener('wheel',e=>{
  e.preventDefault();lbStep(e.deltaY>0?1:-1);},{passive:false});
document.addEventListener('keydown',e=>{
  if(!lb)return;
  if(e.key==='Escape')lbClose();
  else if(e.key==='ArrowRight')lbStep(1);
  else if(e.key==='ArrowLeft')lbStep(-1);
});
apply();
"""


def _u(rel):
    return quote((rel or "").replace(os.sep, "/"))


def _kb(n):
    if not n:
        return "?"
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f} GB"
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _worst(statuses):
    for s in ("failed", "pending", "dup", "skipped"):
        if s in statuses:
            return s
    return "done"


def _dup_root(r, by_id):
    """顺着 dup_of 找到真正的原件（重复的重复这种链最多跟 5 跳）。

    两条互相指着对方（各自都把对方当原件）是老版本重发逻辑留下的环：那样谁都不是原件，
    两张卡会一起从画廊里消失。环上取消息号最小的那条当原件，两条都落回同一张卡。
    """
    seen, cur = [], r
    d = cur["dup_of"]
    while d and d in by_id and d not in seen and len(seen) < 5:
        seen.append(d)
        cur = by_id[d]
        d = cur["dup_of"]
    if d and d in seen:
        return min([r] + [by_id[x] for x in seen], key=lambda x: x["msg_id"])
    return cur


def _member(r, revs):
    """一条成员。

    编辑换过媒体的行：卡片上显示**原图**（最早那条历史版本），现在那份挂在 now 上；
    「已编辑」展开里按 原图 → 每次编辑前 → 现在 全列出来。归档的就是当时发出来的东西，
    所以默认看原图。revs 为空（导出静态页时就是这样）就照原样，显示当前那份。
    原图没有文件就不硬当主图（迟到编辑换媒体的典型：原版还没来得及下载就被换掉，
    线上 699714 那两张卡挂着永远的「排队中」就是这么来的）——主图回落到当前那份。
    """
    # 文件地址一律 path 优先：path 是内容寻址的「当前真身」，永远新鲜；link 只是
    # by-author 目录里的陈列名（名字带旧消息号/旧扩展名，回退一版之类的操作会让它
    # 过期）—— 优先 link 曾把图片时代的 .jpg 名字喂给视频元素（No video with
    # supported format 那个 bug）。link 照旧导出/资源管理器里用，只是不再上网。
    m = {"msg_id": r["msg_id"], "kind": r["kind"], "thumb": r["thumb"],
         "file": r["path"] or r["link"], "width": r["width"], "height": r["height"],
         "size": r["size"], "status": r["status"], "note": r["note"],
         "permalink": r["permalink"], "edited": r["edited"],
         "deleted": r["deleted"], "star": r["star"]}
    if not revs:
        return m
    usable = next((x for x in revs if x["path"]), None)   # 最早**有文件**的那版当主图
    m["now"] = {"kind": m["kind"], "file": m["file"], "thumb": m["thumb"],
                "size": m["size"], "width": m["width"], "height": m["height"]}
    if usable is not None:
        m["kind"], m["file"], m["thumb"] = usable["kind"], usable["path"], usable["thumb"]
        m["size"], m["width"], m["height"] = usable["size"], usable["width"], usable["height"]
    m["revs"] = [{"seq": x["seq"], "kind": x["kind"], "file": x["path"], "thumb": x["thumb"],
                  "size": x["size"], "caption": x["caption"], "edited_at": x["edited_at"]}
                 for x in revs]
    return m


def group_items(rows, revs=None):
    """把一条条消息按相册聚成卡片。

    相册 = 同一个 grouped_id 的多条消息，可以是多张图、多个视频、或混合。
    一条都没有 grouped_id 的消息自己成一组。

    内容重复的行（dup_of 有值：同一个文件被重发）不单独成卡，挂到原件那张卡上，
    原件显示「重复 ×N」—— 去重是内容级的，但重复那条的原始消息链接一条都不丢。
    """
    by_id = {r["msg_id"]: r for r in rows}
    dups, keep = {}, []
    for r in rows:
        root = _dup_root(r, by_id)
        if root is r:
            keep.append(r)
        else:
            dups.setdefault(root["msg_id"], []).append(r)

    groups, order = {}, []
    for r in keep:
        gid = r["grouped_id"]
        key = f"album:{gid}" if gid else f"msg:{r['chat_id']}:{r['msg_id']}"
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"key": key, "grouped_id": gid,
                               "date": r["date"], "size": 0, "caption": "",
                               "dups": [], "_rows": []}
            order.append(key)
        g["date"] = max(g["date"], r["date"])
        g["size"] += r["size"] or 0
        if not g["caption"] and r["caption"]:
            g["caption"] = r["caption"]
        g["_rows"].append(r)

    out = []
    for key in order:
        g = groups[key]
        rs = sorted(g.pop("_rows"), key=lambda r: r["msg_id"])   # 相册内按消息号排，跟客户端一致
        first = rs[0]                                            # 作者/话题/链接都取第一条
        g["author_id"], g["author_name"] = first["author_id"], first["author_name"]
        g["topic_id"], g["topic_title"] = first["topic_id"], first["topic_title"]
        g["permalink"] = first["permalink"]
        g["members"] = [_member(r, (revs or {}).get(r["msg_id"])) for r in rs]
        g["status"] = _worst([m["status"] for m in g["members"]])
        g["note"] = next((m["note"] for m in g["members"] if m["note"]), None)
        g["kind"] = g["members"][0]["kind"] if len(g["members"]) == 1 else "album"
        g["kinds"] = sorted({m["kind"] for m in g["members"]})
        g["edited"] = max(((m["edited"] or 0) for m in g["members"] if not m["deleted"]),
                          default=max((m["edited"] or 0) for m in g["members"]))
        g["caption_before"] = next((r["caption_before"] for r in rs if r["caption_before"]),
                                   None)
        g["deleted"] = any(m["deleted"] for m in g["members"])
        g["star"] = any(m["star"] for m in g["members"])
        out.append(g)

    where = {m["msg_id"]: g for g in out for m in g["members"]}
    for mid, rs in dups.items():
        g = where.get(mid)
        if g is None:
            # 原件不在这一页：这几条按自己的相册/消息号成卡，不能凭空消失。
            # 实际不会走到 —— _dup_root 只会返回本页的行，而本页的行一定都在 where 里。
            for extra in group_items([dict(r, dup_of=None) for r in rs]):
                out.append(extra)
            continue
        # 累加不是覆盖：一个相册里可能有好几条「原件」各自带着重复行，
        # 覆盖的话只有最后一个原件的重复行留得下来，其余的从画廊里消失。
        g["dups"] = (g.get("dups") or []) + [
            {"msg_id": r["msg_id"], "permalink": r["permalink"],
             "date": r["date"], "author_name": r["author_name"]}
            for r in sorted(rs, key=lambda r: r["msg_id"])]
    return out


def _member_html(m):
    """一个成员的格子。有文件的包进 .mb（灯箱入口：data-file/kind/thumb），没有就占位。"""
    if m["kind"] == "video":
        if not m["file"]:
            return f'<div class="none">{html.escape(m["note"] or m["status"])}</div>'
        poster = f' poster="{_u(m["thumb"])}"' if m["thumb"] else ""
        return (f'<a class="mb" data-kind="video" data-file="{_u(m["file"])}" '
                f'data-thumb="{_u(m["thumb"] or "")}" href="{_u(m["file"])}">'
                f'<video preload="none" src="{_u(m["file"])}"{poster}></video></a>')
    if m["thumb"] or m["file"]:
        src = m["thumb"] or m["file"]              # 缩略图没抓到就直接用文件本身
        return (f'<a class="mb" data-kind="photo" data-file="{_u(m["file"] or src)}" '
                f'data-thumb="{_u(m["thumb"] or "")}" href="{_u(m["file"] or src)}">'
                f'<img loading="lazy" src="{_u(src)}" alt=""></a>')
    return f'<div class="none">{html.escape(m["note"] or m["status"])}</div>'


def _revs_html(g):
    """带版本的成员：「已编辑」展开里列 原图 → 每次编辑前 → 现在。

    和网页端那个展开同名同义（网页端是 JS 画、这里用 <details>，都不用点进二级页），
    每张是普通链接，点开就是那一版的文件。
    """
    # 编辑历史归属活着的成员：已删除成员的历史不算到留守卡头上
    m = next((x for x in g["members"] if not x.get("deleted") and x.get("revs")),
             next((x for x in g["members"] if x.get("revs")), None))
    if not m:
        return ""
    shots = [(("原图" if i == 0 else f"第 {i + 1} 次编辑前"), r)
             for i, r in enumerate(m["revs"])]
    if m.get("now"):
        shots.append(("现在", m["now"]))
    cells = "".join(
        f'<div class="revcell"><a href="{_u(s["file"] or s["thumb"])}" target="_blank">'
        f'<img loading="lazy" src="{_u(s["thumb"] or s["file"])}" alt="">'
        + ('<span class="play">▶</span>' if s["kind"] == "video" else "")
        + f'</a><div class="revcap">{html.escape(label)}</div></div>'
        for label, s in shots)
    return (f'<details class="revs"><summary>已编辑</summary>'
            f'<div class="revstrip">{cells}</div></details>')


def _card(g):
    n = min(len(g["members"]), 4)
    cells = "".join(_member_html(m) for m in g["members"][:4])
    extra = (f'<span class="alb">共 {len(g["members"])} 个</span>'
             if len(g["members"]) > 1 else "")
    if g.get("dups"):
        extra += (f'<span class="alb" title="同一个文件重发了 {len(g["dups"])} 次，'
                  f'磁盘上只存一份">重复 ×{len(g["dups"]) + 1}</span>')

    revs = _revs_html(g)
    au_name = g["author_name"] or "?"
    where = g["topic_title"] or (f"话题 {g['topic_id']}" if g["topic_id"] else "General")
    marks = [f'<a href="{html.escape(g["permalink"])}" target="_blank">原消息</a>'
             if g["permalink"] else ""]
    if g["status"] != "done":
        marks.append(f'<span class="bad">{html.escape(g["status"])}'
                     f'{": " + html.escape(g["note"]) if g["note"] else ""}</span>')
    if g["deleted"]:
        marks.append('<span class="bad">已删除</span>')
    elif g["edited"] and not revs:                # 只改了说明、没换媒体：没有版本条可展开
        marks.append('<span class="k">已编辑</span>')

    blob = " ".join(str(x or "") for x in
                    (g["author_name"], g["author_id"], g["caption"], g["topic_title"],
                     g["topic_id"], g["grouped_id"], g["kinds"], g["status"])).lower()
    return (f'<figure data-k="{",".join(g["kinds"])}" data-status="{g["status"]}" '
            f'data-deleted="{1 if g["deleted"] else 0}" '
            f'data-edited="{1 if g["edited"] else 0}" '
            f'data-star="{1 if g["star"] else 0}" '
            f'data-au="{html.escape(au_name, quote=True)}" '
            f'data-tp="{html.escape(where, quote=True)}" '
            f'data-blob="{html.escape(blob, quote=True)}">'
            f'<div class="grid n{n}">{cells}</div><figcaption>'
            f'<div class="cap">{html.escape(g["caption"] or "")}</div>'
            f'<div class="meta"><span class="k">{g["kind"]}</span> {extra} {_when(g["date"])}</div>'
            f'<div class="meta"><span class="au" data-au="{html.escape(au_name, quote=True)}" '
            f'title="只看这个作者">{html.escape(au_name)} <code>{g["author_id"]}</code></span>'
            f' · <span class="au" data-tp="{html.escape(where, quote=True)}" '
            f'title="只看这个话题">{html.escape(where)}</span> · {_kb(g["size"])}</div>'
            f'<div class="meta">{" ".join(x for x in marks if x)}</div>'
            f'{revs}'
            f'</figcaption></figure>')


def render(path, rows, title, chat_id, revs=None):
    """把索引渲染成一份自包含的静态 HTML，关掉程序也能看。"""
    groups = group_items(rows, revs)
    kinds = {}
    for g in groups:
        for k in g["kinds"]:
            kinds[k] = kinds.get(k, 0) + 1
    done = [g for g in groups if g["status"] == "done"]
    counts = {}
    for g in groups:
        counts[g["status"]] = counts.get(g["status"], 0) + 1
    authors = sorted({(g["author_name"] or "?") for g in groups})
    topics = sorted({(g["topic_title"]
                      or (f"话题 {g['topic_id']}" if g["topic_id"] else "General"))
                     for g in groups})

    def _opts(pairs):
        return "".join(f'<option value="{html.escape(v, quote=True)}">'
                       f'{html.escape(label)}</option>' for v, label in pairs)

    head = (f'<h1>{html.escape(title)} <span class="stats">({chat_id})</span></h1>'
            f'<div class="stats">共 {len(groups)} 条'
            f'（相册 {sum(1 for g in groups if len(g["members"]) > 1)} 组）'
            f' · {"、".join(f"{k} {v}" for k, v in sorted(kinds.items())) or "无"}'
            f' · 已存 {_kb(sum(g["size"] for g in done))}'
            f' · {"、".join(f"{k} {v}" for k, v in sorted(counts.items()))}'
            f' · 生成于 {time.strftime("%Y-%m-%d %H:%M")}</div>'
            f'<div class="bar">'
            f'<input type="search" id="q" placeholder="过滤：作者 / 昵称 / 话题 / 说明">'
            f'<select id="k"><option value="">全部类型</option>'
            + _opts((k, k) for k in sorted(kinds))
            + f'</select><select id="s"><option value="">全部状态</option>'
            + _opts([("done", "只看下好的"), ("pending", "只看等待中"),
                     ("skipped", "只看跳过的"), ("failed", "只看失败"),
                     ("deleted", "只看已删除"), ("edited", "只看已编辑"),
                     ("star", "只看收藏")])
            + f'</select><select id="au"><option value="">全部作者</option>'
            + _opts((a, a) for a in authors)
            + f'</select><select id="tp"><option value="">全部话题</option>'
            + _opts((t, t) for t in topics)
            + f'</select><span id="n"></span></div>')

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("<!doctype html><html lang=zh><head><meta charset=utf-8>"
                 "<meta name=viewport content='width=device-width,initial-scale=1'>"
                 f"<title>{html.escape(title)} 画廊</title>"
                 f"<style>{CSS}</style></head><body>"
                 f"<header>{head}</header><main>"
                 + "".join(_card(g) for g in groups)
                 + f"</main><script>{JS}</script></body></html>")
    return len(groups)
