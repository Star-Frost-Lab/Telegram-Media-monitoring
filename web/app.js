'use strict';
const $ = s => document.querySelector(s);

/* ---------- 窄屏判定 ---------- */
/* 不用 @media 判断窄屏：手机上视口可能被报成桌面宽度（「桌面版网站」开着、或者放大过），
   媒体查询就整块失效、看着跟桌面一样。取 innerWidth 和 screen.width 里小的那个。
   外壳高度不在这里算：visualViewport 在 Chrome 的设备模拟里不跟着变，量出来的值没法验。 */
function markNarrow() {
  document.documentElement.classList.toggle('narrow',
    Math.min(innerWidth, screen.width || 9999) <= 860);
  drawDiag();
}
/* 手机上出问题时，截这一行就够定位：视口 / 可视 / 缩放 / 屏幕 / 走的是哪套布局。
   平时不显示（面板里那行是 hide 的），地址后面加 ?diag 才出来 —— 日常界面用不着它。 */
function drawDiag() {
  const el = $('#diag');
  if (!el || !/[?&]diag/.test(location.search)) return;
  el.classList.remove('hide');
  const vv = window.visualViewport;
  el.textContent = `视口 ${innerWidth}×${innerHeight}`
    + (vv ? `　可视 ${Math.round(vv.width)}×${Math.round(vv.height)} 缩放 ${vv.scale}` : '')
    + `　屏幕 ${screen.width}×${screen.height} @${devicePixelRatio}`
    + `　${document.documentElement.className || '（无类名）'}`;
}
markNarrow();
addEventListener('resize', markNarrow);
// 键盘弹出、双指放大只动可视视口、不动窗口，所以要单独听
if (window.visualViewport) visualViewport.addEventListener('resize', () => { markNarrow(); fitMasonry(); });

/* ---------- 深浅色：跟随系统，手动切了记住 ---------- */
(function () {
  const saved = localStorage.getItem('theme');
  const sys = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  document.documentElement.dataset.theme = saved || sys;
})();
$('#theme').onclick = () => {
  const el = document.documentElement;
  el.dataset.theme = el.dataset.theme === 'dark' ? 'light' : 'dark';
  localStorage.setItem('theme', el.dataset.theme);
};

const state = {chats: [], chat: null, tab: 'list', minMsg: null,
               cards: new Map(), filter: '', up: false, status: null, stats: null,
               warned: false, loading: false, total: 0, more: false,
               pick: false, sel: new Set(), chatsKind: '',
               wtSel: new Set(), wtList: []};   // 话题监听勾选：选中集 + 面板数据
let pollTimers = false;       // 轮询定时器只注册一次（以前每恢复一次就多一套，手机越用越多请求）

async function api(path, opts) {
  const r = await fetch(path, opts);
  // 不是 JSON 就说明局域网密码掉了（服务这时候回的是密码页），直接回登录页；
  // 以前这里只会抛错，页面就永远停在「正在等本地服务…」那句误导的提示上。
  if (!(r.headers.get('Content-Type') || '').includes('json')) {
    location.href = '/';
    throw new Error('需要访问密码');
  }
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j;
}
const post = (p, body) => api(p, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                  body: JSON.stringify(body || {})});

let toastTimer = null;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.remove('hide');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hide'), 2800);
}

const when = ts => new Date(ts * 1000).toLocaleString('zh-CN', {hour12: false}).slice(5, 16);

/* 自己的确认弹窗。原生 confirm 手机上丑、还可能被拦，所以自己画一个。
   Esc / 点背景 / 点「取消」都算取消；默认焦点在「取消」上（破坏性操作别让回车误伤）。 */
function ask(text, yesText) {
  return new Promise(res => {
    const box = $('#ask');
    $('#asktext').textContent = text;
    $('#askyes').textContent = yesText || '删除';
    const key = e => {
      if (e.key === 'Escape') done(false);
      else if (e.key === 'Enter') done(true);
    };
    const done = v => {
      box.classList.add('hide');
      document.removeEventListener('keydown', key);
      $('#askyes').onclick = $('#askno').onclick = box.onclick = null;
      res(v);
    };
    $('#askyes').onclick = () => done(true);
    $('#askno').onclick = () => done(false);
    box.onclick = e => { if (e.target === box) done(false); };
    document.addEventListener('keydown', key);
    box.classList.remove('hide');
    $('#askno').focus();
  });
}
const size = n => !n ? '?' : n >= 1 << 30 ? (n / (1 << 30)).toFixed(2) + ' GB'
  : n >= 1 << 20 ? (n / (1 << 20)).toFixed(1) + ' MB' : (n / 1024).toFixed(0) + ' KB';
const gb = n => (n / (1 << 30)).toFixed(2) + ' GB';
const initial = s => (String(s || '?').trim() || '?').slice(0, 1).toUpperCase();

/* ---------- 连接状态机 ---------- */
function showGate(kind, title, msg) {
  $('#app').classList.add('hide');
  $('#gate').classList.remove('hide');
  $('#gmsg').textContent = msg || '';
  if (kind === 'connecting') {
    if (!$('#gtitle .spin')) $('#gtitle').innerHTML = '<span class="spin"></span> ' + title;
  } else {
    $('#gtitle').textContent = title;
  }
  $('#gsetup').classList.toggle('hide', kind !== 'setup');
  $('#gqr').classList.toggle('hide', kind !== 'login');
  $('#gpw').classList.toggle('hide', kind !== 'password');
  $('#glock').classList.toggle('hide', kind !== 'locked');
  $('#gretry').classList.toggle('hide', kind !== 'offline');
}

const GATE = {
  starting: ['connecting', '正在连接…', '正在用本地 session 连 Telegram。'],
  connecting: ['connecting', '正在连接…', '正在用本地 session 连 Telegram。'],
  setup: ['setup', '先填 api_id', '在 my.telegram.org 申请的那串数字，填一次就存在本机。'],
  login: ['login', '扫码登录', '用手机 Telegram → 设置 → 设备 → 扫描二维码。'],
  expired: ['login', '二维码过期了', '点下面的按钮重新生成。'],
  need_password: ['password', '需要两步验证密码', ''],
  locked: ['locked', '已锁定', '凭证是加密存的，输一次口令才会连 Telegram。'],
  offline: ['offline', '连不上 Telegram', ''],
  error: ['login', '登录出错', ''],
};

function applyStatus(s) {
  state.status = s;
  paintProgress();          // 下载进度每 2.5 秒跟着状态刷一次，不用重画卡片
  if (s.state === 'ok') {
    if (!state.up) {
      state.up = true;
      state.warned = false;
      $('#gate').classList.add('hide');
      $('#app').classList.remove('hide');
      loadChats();
      pollStats();
      if (!pollTimers) {
        pollTimers = true;
        setInterval(pollStats, 10000);
        setInterval(pollLive, 2000);
      }
    }
    return;
  }
  if (s.state === 'offline' && state.up) {          // 跑着跑着断了：别把界面收走，提示一下
    if (!state.warned) { state.warned = true; toast('连不上 Telegram，正在自动重试…'); }
    return;
  }
  state.up = false;
  // 刚进锁定态：把上一次的报错清掉（提示写在 #lockmsg，状态轮询不会碰它）
  if (s.state === 'locked' && state.gateKind !== 'locked') $('#lockmsg').textContent = '';
  state.gateKind = s.state;
  // api_hash 只在要重新登录时才要：本地有 session 就收着，别摆在首屏。
  // 只自动展开、不自动收起 —— 状态每 2.5 秒轮询一次，收起会把用户的手动展开打回去。
  if (s.state === 'setup' && (!s.has_session || /api_hash/.test(s.error || ''))) $('#gadv').open = true;
  const m = GATE[s.state] || ['connecting', s.state, s.error || ''];
  let msg = s.error || m[2];                    // 后端给了具体原因就用它
  if (s.state === 'offline') {
    msg = (s.error || '') + '　检查一下代理是不是开着。';
  }
  if (s.state === 'locked' && s.local === false) {
    msg = '口令只能在这台电脑上输（网页是明文 HTTP，不能走局域网）。';
  }
  showGate(m[0], m[1], msg);
}

async function tick() {
  try { applyStatus(await api('/api/status')); }
  catch (e) { showGate('connecting', '正在等本地服务…', '如果这个提示一直不走，看 _run.log。'); }
}

/* api_id 是纯数字，api_hash 是 32 位十六进制。把 id 填进 hash 栏是很常见的错。 */
function checkCreds(id, hash) {
  if (!id) return 'api_id 必填（my.telegram.org 上那串数字）';
  if (!/^\d+$/.test(id)) return 'api_id 只能是数字';
  if (hash && hash === id) return 'api_hash 栏里填的好像是 api_id，这两个不一样';
  if (hash && hash.length !== 32) return 'api_hash 是 32 位十六进制';
  return null;
}

$('#gsave').onclick = async () => {
  const id = $('#gid').value.trim(), hash = $('#ghash').value.trim();
  const bad = checkCreds(id, hash);
  if (bad) return toast(bad);
  $('#gsave').disabled = true;
  try {
    applyStatus(await post('/api/config', {api_id: id, api_hash: hash}));
    toast(hash ? '已保存，以后不用再填' : '已保存（api_hash 留空，重新登录时才要）');
  } catch (e) { toast('保存失败：' + e.message); }
  finally { $('#gsave').disabled = false; }
};

$('#qrbtn').onclick = async () => {
  $('#qrbtn').disabled = true;
  $('#gmsg').textContent = '正在生成二维码…';
  try {
    const r = await post('/api/login/qr');
    if (r.state === 'setup') { applyStatus({state: 'setup', error: r.error}); return; }
    $('#qrbox').innerHTML = '<img alt="二维码">';
    $('#qrbox img').src = r.png;
    $('#gmsg').textContent = '用手机 Telegram → 设置 → 设备 → 扫描二维码。窗口别关。';
  } catch (e) { $('#gmsg').textContent = '生成失败：' + e.message; }
  finally { $('#qrbtn').disabled = false; }
};

$('#pwbtn').onclick = async () => {
  const r = await post('/api/login/password', {password: $('#pw').value});
  if (r.ok) { toast('登录成功'); tick(); } else { $('#gmsg').textContent = r.error; }
};

$('#gretry').onclick = async () => {
  $('#gretry').disabled = true;
  try { applyStatus(await post('/api/retry')); } catch (e) { toast(e.message); }
  finally { $('#gretry').disabled = false; }
};

/* 启动口令：解锁、以及忘口令时重置。口令只在本机输得通（服务端也只认本机）。
   提示写在 #lockmsg 里而不是 #gmsg：状态每 2.5 秒轮询一次会把 #gmsg 覆盖掉。 */
$('#lockbtn').onclick = async () => {
  const pw = $('#lockpw').value;
  if (!pw) return toast('先输口令');
  $('#lockbtn').disabled = true;
  $('#lockmsg').textContent = '';
  try {
    // api() 见到 error 就抛，所以走得到这里就是解开了
    const r = await post('/api/unlock', {passcode: pw});
    $('#lockpw').value = '';
    toast('已解锁');
    applyStatus(r.status);
  } catch (e) { $('#lockmsg').textContent = e.message; }
  finally { $('#lockbtn').disabled = false; }
};
$('#lockpw').addEventListener('keydown', e => { if (e.key === 'Enter') $('#lockbtn').click(); });
$('#lockreset').onclick = async () => {
  if (!await ask('重置会扔掉加密的 session 和 api_hash：要重新扫码登录、api_hash 也要重填。确定吗？',
                 '重置')) return;
  try { applyStatus(await post('/api/session/reset')); toast('已重置，请重新登录'); }
  catch (e) { $('#lockmsg').textContent = '重置失败：' + e.message; }
};

/* ---------- 群列表 ---------- */
let avTimer = null, avRounds = 0;
async function loadChats(force) {
  try {
    const r = await api('/api/chats' + (force ? '?force=1' : ''));
    state.chats = r.chats || [];
    renderChats();
    // 回到上次看的群（找不到就照旧空着）；每台设备记自己的，存 localStorage
    const last = localStorage.getItem('arc.chat');
    const prev = last && state.chats.find(c => String(c.chat_id) === last);
    if (prev && (!state.chat || state.chat.chat_id !== prev.chat_id)) selectChat(prev);
    // 头像在服务端后台补：列表先上，缺头像的隔 15s 看一眼（走缓存，免费），
    // 到齐或看满 5 次就停
    clearTimeout(avTimer);
    if (state.chats.some(c => !c.avatar) && avRounds++ < 5)
      avTimer = setTimeout(() => loadChats(false), 15000);
    else avRounds = 0;
  } catch (e) { toast('拉会话列表失败：' + e.message); }
}

/* 左栏「全部 / 群组 / 频道」筛选 chips：纯前端过滤，不另发请求 */
for (const b of document.querySelectorAll('#kindchips button')) {
  b.onclick = () => {
    state.chatsKind = b.dataset.k;
    for (const x of document.querySelectorAll('#kindchips button'))
      x.classList.toggle('on', x === b);
    renderChats();
  };
}

function renderChats() {
  const q = $('#search').value.trim().toLowerCase();
  const box = $('#chats');
  box.innerHTML = '';
  for (const c of state.chats) {
    const title = c.title || String(c.chat_id);
    if (q && !title.toLowerCase().includes(q) && !String(c.chat_id).includes(q)) continue;
    if (state.chatsKind && c.kind !== state.chatsKind) continue;   // 群/频道 chips
    const on = !!(c.watch && c.watch.enabled);
    const d = document.createElement('div');
    d.className = 'chat' + (state.chat && state.chat.chat_id === c.chat_id ? ' sel' : '');
    const av = document.createElement('span');
    av.className = 'avatar';
    if (c.avatar) {                          // 真头像；挂了回退首字母
      const im = document.createElement('img');
      im.src = c.avatar;
      im.onerror = () => im.remove();
      av.appendChild(im);
    }
    av.appendChild(document.createTextNode(initial(title)));
    if (c.kind === '频道') {                  // 「全部」模式下也能一眼分出频道
      const k = document.createElement('i');
      k.className = 'kmark';
      k.textContent = '频';
      av.appendChild(k);
    }
    const t = document.createElement('span');
    t.className = 't';
    t.textContent = title;
    const live = document.createElement('span');
    live.className = 'live' + (on ? ' on' : '');
    d.append(av, t, live);
    d.title = title + '  ' + c.chat_id + (c.forum ? '  (话题群)' : '');
    d.onclick = () => selectChat(c);
    box.appendChild(d);
  }
}

async function loadAuthors(c) {
  /* 换群时 selectChat 已把 state.author 清掉；删除后重灌下拉时保留当前筛选：
     作者还在就重新选中他（条数/占用已是新数字），被删空（不在列表里了）才退回「全部作者」 */
  try {
    const r = await api('/api/authors?chat=' + c.chat_id);
    const had = state.author;
    const sel = $('#author');
    sel.innerHTML = '<option value="">全部作者</option>';
    let back = '';
    for (const a of r.authors || []) {
      /* 0 条的（删空的）只有正选着的留在下拉里 —— 停在他这档删完不弹回「全部」，
         一换开到别的筛选（pruneZero 重灌）他就消失。
         顺序由服务端按名称排好（不随删改漂移），前端照摆，滚轮邻居永远稳定 */
      if (!a.n && String(a.author_id) !== had) continue;
      const o = document.createElement('option');
      o.value = a.author_id;
      o.textContent = (a.name || a.author_id) + '（' + a.n + ' 条 · '
        + (a.bytes ? size(a.bytes) : '0 B') + '）';
      if (String(a.author_id) === had) back = String(a.author_id);
      sel.appendChild(o);
    }
    if (back) sel.value = back;
    else if (had) {              // 这位作者的条目删光了：退回全部，列表重拉
      state.author = '';
      loadItems(true);
    }
  } catch (e) { /* 拉不到就空着，不影响别的 */ }
  syncFilter();
}

async function loadTopics(c) {
  /* 同 loadAuthors：换群时 selectChat 已清空筛选；删完重灌时保留当前筛选，
     0 条的话题只有正选着的留（删空不弹回「全部话题」，一换开就消失） */
  try {
    const r = await api('/api/topics?chat=' + c.chat_id);
    const had = state.topic;
    const sel = $('#topic');
    sel.innerHTML = '<option value="">全部话题</option>';
    state.wtList = [['general', 'General（默认话题）', null]];   // 勾选面板的数据
    let back = '';
    for (const t of r.topics || []) {
      /* 同 loadAuthors：0 条的话题只有正选着的留；顺序由服务端按标题排好 */
      if (!t.n && String(t.topic_id) !== had) continue;
      const o = document.createElement('option');
      o.value = t.topic_id;
      o.textContent = (t.title || ('话题 ' + t.topic_id)) + '（' + t.n + ' 条）';
      if (String(t.topic_id) === had) back = String(t.topic_id);
      sel.appendChild(o);
      state.wtList.push([String(t.topic_id),
                         (t.title || '话题 ' + t.topic_id), t.n]);
    }
    setPickTopics(((state.chat && state.chat.watch && state.chat.watch.topics) || '')
      .split(',').filter(x => x));
    if (back) sel.value = back;
    else if (had) {              // 这个话题的条目删光了：退回全部，列表重拉
      state.topic = '';
      loadItems(true);
    }
  } catch (e) { /* 拉不到就空着，不影响别的 */ }
  syncFilter();
}

async function selectChat(c) {
  state.chat = c;
  localStorage.setItem('arc.chat', String(c.chat_id));
  state.minMsg = null;
  state.total = 0;
  state.more = false;
  state.author = '';                       // 换群筛选重置（以前靠 loadAuthors/loadTopics 顺手清）
  state.topic = '';
  state.cards.clear();
  $('.side').classList.remove('open');   // 窄屏：选完就把全屏群列表收起来，别挡着内容
  closePanels();                         // 换群了，设置/筛选面板收起来
  setPick(false);                        // 换群也把编辑模式和选择一起清掉
  $('#items').innerHTML = '';
  $('#title').textContent = (c.title || c.chat_id) + '  (' + c.chat_id + ')';
  applyKindUI(c);
  loadAuthors(c);
  loadTopics(c);
  $('#bfstate').textContent = '';
  const w = c.watch || {};
  $('#kphoto').checked = !w.kinds || w.kinds.includes('photo');
  $('#kvideo').checked = !w.kinds || w.kinds.includes('video');
  $('#maxmb').value = w.max_mb || 100;
  $('#wauth').value = (w.authors || '').replace(/,/g, ', ');
  setPickTopics((w.topics || '').split(',').filter(x => x));
  setToggle(!!w.enabled);
  renderChats();
  await loadItems(true);
}

/* 会话类型差异化：频道没有发信人（白名单/作者筛选本来就没意义），
   非论坛群没有话题。数据（kind/forum）服务端早就有，这里只管显隐。 */
function applyKindUI(c) {
  const chan = c.kind === '频道';
  $('#wauthrow').classList.toggle('hide', chan);
  $('#fau').classList.toggle('hide', chan);
  $('#ftp').classList.toggle('hide', chan || !c.forum);
  $('#wtopicrow').classList.toggle('hide', chan || !c.forum);
}

function setPickTopics(ids) {              // 把 watch.topics 灌进「只收这些话题」
  state.wtSel = new Set(ids);              // general 代表 General；空集 = 全部话题
  renderWTopic();
}

/* 话题多选：假下拉 + 勾选面板（原生 multiple 取消要按 Ctrl，等于没法用） */
function renderWTopic() {
  const sel = state.wtSel;
  const box = $('#wtopic');
  box.querySelector('.v').textContent =
    sel.size ? '已选 ' + sel.size + ' 个话题' : '全部话题';
  const list = $('#wlist');
  list.innerHTML = '';
  const all = document.createElement('div');
  all.className = 'witem all' + (sel.size ? '' : ' on');
  all.title = '不勾任何一条 = 收全部话题；点这里清空选择';
  all.innerHTML = '<span class="box">✓</span>全部';
  all.onclick = async () => { sel.clear(); renderWTopic(); await saveWatch(); };
  list.appendChild(all);
  for (const [id, title, n] of state.wtList) {
    const it = document.createElement('div');
    it.className = 'witem' + (sel.has(id) ? ' on' : '');
    it.innerHTML = '<span class="box">✓</span>' + escapeHtml(title)
      + (n != null ? '<span class="n">' + n + ' 条</span>' : '');
    it.onclick = async () => {             // 再点一下同一个就是取消选中
      sel.has(id) ? sel.delete(id) : sel.add(id);
      renderWTopic();
      await saveWatch();
    };
    list.appendChild(it);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function watchBody(state_chat, extra) {
  /* /api/watch 的公共体 + 作者白名单：白名单按非数字拆开，只留数字 ID。
     话题多选恒带上：没选任何话题 = 全部话题（空串） */
  const kinds = [];
  if ($('#kphoto').checked) kinds.push('photo');
  if ($('#kvideo').checked) kinds.push('video');
  const ids = $('#wauth').value.split(/[^\d]+/).filter(x => x);
  return Object.assign({chat_id: state_chat.chat_id, title: state_chat.title,
                        kinds, max_mb: +$('#maxmb').value || 100,
                        topics: [...state.wtSel], authors: ids}, extra);
}

function setToggle(on) {
  const b = $('#toggle');
  b.textContent = on ? '停止监听' : '开始监听';
  b.className = 'primary' + (on ? ' on' : '');
  $('#wstate').textContent = on ? '监听中：新消息符合类型就自动下载' : '未监听';
}

$('#toggle').onclick = async () => {
  if (!state.chat) return;
  const on = !$('#toggle').classList.contains('on');
  if (on && !($('#kphoto').checked || $('#kvideo').checked)) {
    toast('至少勾一种类型'); return; }
  try {
    const r = await post('/api/watch', watchBody(state.chat, {enabled: on}));
    state.chat.watch = {enabled: !!r.watch.enabled, max_mb: r.watch.max_mb,
                        authors: r.watch.authors, topics: r.watch.topics,
                        kinds: (r.watch.kinds || '').split(',')};
    setToggle(!!r.watch.enabled);
    renderChats();
    toast(on ? '开始监听，等新消息进来' : '已停止监听');
  } catch (e) { toast('操作失败：' + e.message); }
};

// 上限/类型/白名单/话题一改就存（以前只有点「开始/停止监听」那一刀才存，改完不点就刷新→丢设置，
// 手机端也因此看不到新值）。POST 不带 enabled —— set_watch 里 enabled 缺省就不动，
// 监听开关照旧只归开关按钮管。类型一个都没勾就不存（存了也没有 false/false/false 的语义）。
async function saveWatch() {
  if (!state.chat) return;
  if (!($('#kphoto').checked || $('#kvideo').checked)) {
    toast('至少勾一种类型'); return; }
  try {
    const r = await post('/api/watch', watchBody(state.chat));
    state.chat.watch = {enabled: !!r.watch.enabled, max_mb: r.watch.max_mb,
                        authors: r.watch.authors, topics: r.watch.topics,
                        kinds: (r.watch.kinds || '').split(',')};
    renderChats();
    toast('设置已保存');
  } catch (e) { toast('保存失败：' + e.message); }
}

for (const id of ['#maxmb', '#kphoto', '#kvideo', '#wauth'])
  $(id).addEventListener('change', saveWatch);

/* 话题勾选面板的开关：点触发钮开/收，点外面收 */
$('#wtopicbtn').onclick = e => { e.stopPropagation(); $('#wtopic').classList.toggle('open'); };
document.addEventListener('click', e => {
  if (!$('#wtopic').contains(e.target)) $('#wtopic').classList.remove('open');
});

$('#bfbtn').onclick = async () => {
  if (!state.chat) return;
  const hours = +$('#bfhours').value || 3;
  $('#bfbtn').disabled = true;
  $('#bfstate').textContent = '正在往回翻…';
  try {
    const r = await post('/api/backfill', {chat_id: state.chat.chat_id, hours: hours});
    $('#bfstate').textContent = '补了 ' + r.added + ' 条，已排队下载';
    toast('补历史完成：' + r.added + ' 条');
    await loadItems(true);
  } catch (e) { $('#bfstate').textContent = '失败：' + e.message; }
  finally { $('#bfbtn').disabled = false; }
};

/* ---------- 条目 ---------- */
function mediaUrl(rel) { return '/media/' + state.chat.chat_id + '/' + encodeURI(rel); }

function cell(m, g) {
  const box = document.createElement('div');
  box.className = 'cell';
  // 按 Telegram 给的宽高先占位：图片还没加载完也不跳版，瀑布流的高度才算得准
  if (m.width && m.height) box.style.aspectRatio = m.width + ' / ' + m.height;
  if (m.kind === 'video' && m.file) {
    // 卡片上不放能播的 <video>：手机上点它只会内联播放，永远进不了灯箱，也翻不了下一条。
    // 用封面 + 播放角标，点开进灯箱（灯箱里自动播、能左右翻）。
    const v = document.createElement('video');
    v.muted = true;
    v.playsInline = true;
    v.preload = 'metadata';
    v.src = mediaUrl(m.file);
    if (m.thumb) v.poster = mediaUrl(m.thumb);
    const a = document.createElement('a');
    a.href = mediaUrl(m.file);
    a.appendChild(v);
    a.onclick = e => { e.preventDefault(); light(g, m); };
    box.appendChild(a);
    const p = document.createElement('span');
    p.className = 'playmark';
    p.textContent = '▶';
    box.appendChild(p);
    return box;
  }
  // 缩略图偶尔抓不到（Telegram 就是不给这张）：图片的话直接拿文件本身当图，
  // 别只剩一句占位文字 —— 图片本身也不大，能显示比省那点流量重要。
  const src = m.thumb || (m.kind === 'photo' ? m.file : null);
  if (src) {
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.src = mediaUrl(src);
    if (m.file) {
      const a = document.createElement('a');
      a.href = mediaUrl(m.file);
      a.appendChild(img);
      a.onclick = e => { e.preventDefault(); light(g, m); };
      box.appendChild(a);
      return box;
    }
    // 还没下好：别把缩略图当文件链接挂上去（复制出来会是个 .jpg 地址，点开也不是视频）
    const d = document.createElement('div');
    d.className = 'thumbonly';
    d.appendChild(img);
    d.onclick = () => light(g, m);
    box.appendChild(d);
    const p = document.createElement('span');
    p.className = 'pendingmark';
    p.dataset.msg = m.msg_id;
    const gone = m.status === 'failed' && (m.note || '').includes('消息已经不在');
    p.textContent = m.status === 'dup' ? '重复'
                  : m.status === 'failed' ? (gone ? '消息已不在' : '下载失败')
                  : '排队中';
    if (m.status === 'failed' && !gone) {          // 手动重试：放回队列，行和缩略图不动
      const b = document.createElement('button');
      b.className = 'ghost';
      b.textContent = '重试';
      b.onclick = async () => {
        try {
          const r = await post('/api/retryone', {chat_id: state.chat.chat_id,
                                                 msg_id: m.msg_id});
          if (r.error) return toast(r.error);
          toast('已重新排队下载');
          loadItems(true);
        } catch (e) { toast('重试失败：' + e.message); }
      };
      p.appendChild(b);
    }
    box.appendChild(p);
    return box;
  }
  const ph = document.createElement('div');
  ph.className = 'none';
  ph.textContent = m.note || m.status;
  box.appendChild(ph);
  return box;
}

// 下载进度：只改角标文字，不重画卡片 —— 重画会把正在播的视频打断
function paintProgress() {
  const dl = (state.status && state.status.downloading) || {};
  for (const mark of document.querySelectorAll('#items .pendingmark')) {
    const d = dl[mark.dataset.msg];
    if (!d) continue;
    mark.textContent = '下载中 ' + size(d.done) + ' / ' + size(d.total);
  }
}

/* 瀑布流：grid-auto-rows 是 1px，每张卡按自己的实测高度占 N 行。
   用 span N 而不是写死高度，卡片内容一变（说明换行、图片加载完）也能跟着变。 */
function fitMasonry() {
  const box = $('#items');
  if (box.classList.contains('stats')) return;    // 统计页是表格，不是瀑布流
  const gap = parseFloat(getComputedStyle(box).rowGap) || 14;
  for (const el of box.children) {
    if (!el.__g) continue;                        // 末尾的哨兵不是卡片，别给它算高度
    const h = el.getBoundingClientRect().height;
    if (!h) continue;
    el.style.gridRowEnd = 'span ' + Math.ceil((h + gap) / (1 + gap));
  }
}

/* ---------- 分享 / 删除 ---------- */

function copyText(s) {
  if (navigator.clipboard) return navigator.clipboard.writeText(s).then(() => toast('已复制'));
  const ta = document.createElement('textarea');       // 老浏览器兜底
  ta.value = s;
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  ta.remove();
  toast('已复制');
}

const memberFile = g => (g.members.find(m => m.file) || {}).file;
const memberAbs = g => (g.members.find(m => m.abs) || {}).abs;

function shareMenu(g, anchor) {
  const box = $('#sharemenu');
  const file = memberFile(g);
  if (!file) { toast('还没下载完，没有链接可以分享'); return; }   // 文件没有：别弹一个空盒子（那个「横条」）
  box.innerHTML = '';
  const items = [
    ['保存 / 下载到本机', () => { location.href = mediaUrl(file) + '?dl=1'; }, !!file],
    ['复制网页链接', () => copyText(location.origin + mediaUrl(file)), !!file],
  ];
  for (const [label, fn, show] of items) {
    if (!show) continue;
    const b = document.createElement('button');
    b.className = 'ghost';
    b.textContent = label;
    b.onclick = async () => { box.classList.add('hide'); await fn(); };
    box.appendChild(b);
  }
  const r = anchor.getBoundingClientRect();
  box.classList.remove('hide');
  box.style.left = Math.max(8, Math.min(r.left, innerWidth - box.offsetWidth - 8)) + 'px';
  box.style.top = Math.max(8, Math.min(r.bottom + 6, innerHeight - box.offsetHeight - 8)) + 'px';
}

// 菜单得能关掉：点别处、滚动、改窗口大小都要收起来
addEventListener('resize', () => { $('#sharemenu').classList.add('hide'); fitMasonry(); });
addEventListener('scroll', () => $('#sharemenu').classList.add('hide'), true);
document.addEventListener('click', e => {
  if (!$('#sharemenu').contains(e.target) && !e.target.closest('.acts')) {
    $('#sharemenu').classList.add('hide');
  }
});

async function reveal(g, path) {
  const m = g.members.find(x => x.file) || g.members[0];
  try {
    const r = await post(path, {chat_id: state.chat.chat_id, msg_id: m.msg_id});
    if (r.error) toast(r.error);
  } catch (e) { toast('失败：' + e.message); }
}

/* 删完把「按作者」下拉的条数/占用重灌一遍；统计页则整页重画（那边的表格行不会自己消失） */
function refreshAfterDelete() {
  if (state.tab === 'stats') loadStats();
  else loadAuthors(state.chat);
}

/* ---------- 编辑模式：点「选择」进，点卡片选中，底栏批量删 ---------- */
function setPick(on) {
  state.pick = on;
  state.sel.clear();
  $('#items').classList.toggle('pick', on);
  $('#editbtn').classList.toggle('on', on);
  $('#editbtn').textContent = on ? '完成' : '选择';
  $('#pickbar').classList.toggle('hide', !on);
  for (const f of document.querySelectorAll('#items figure.sel')) f.classList.remove('sel');
  paintPick();
}

function paintPick() {
  $('#pickcnt').textContent = '已选 ' + state.sel.size + ' 张';
}

/* capture 阶段拦下点在缩略图区（.grid）里的点击：编辑模式下这是「选中」，不是开灯箱；
   preventDefault+stopPropagation 正好掐掉卡片里 <a> 的默认跳转和 light() */
$('#items').addEventListener('click', e => {
  if (!state.pick) return;
  const fig = e.target.closest('#items figure');
  if (!fig || !fig.__g || !e.target.closest('.grid')) return;
  e.preventDefault();
  e.stopPropagation();
  const key = fig.__g.key;
  if (state.sel.has(key)) { state.sel.delete(key); fig.classList.remove('sel'); }
  else { state.sel.add(key); fig.classList.add('sel'); }
  paintPick();
}, true);

$('#editbtn').onclick = () => {
  if (!state.chat) return toast('先选一个群');
  setPick(!state.pick);
};

$('#pickclear').onclick = () => {
  for (const f of document.querySelectorAll('#items figure.sel')) f.classList.remove('sel');
  state.sel.clear();
  paintPick();
};

$('#pickall').onclick = () => {          // 全选＝把已加载进页面的卡片全标上（还有更老的不算）
  for (const f of document.querySelectorAll('#items figure')) {
    if (!f.__g) continue;
    state.sel.add(f.__g.key);
    f.classList.add('sel');
  }
  paintPick();
};

$('#pickdel').onclick = async () => {
  const ms = [];                 // 存活卡片的成员归拢成一份 msg_ids
  let nMsg = 0, nSize = 0;
  for (const key of state.sel) {
    const c = state.cards.get(key);
    if (!c || !c.el.__g) continue;
    nSize += c.el.__g.size || 0;
    ms.push(...c.el.__g.members.map(m => m.msg_id));
    nMsg++;
  }
  if (!ms.length) return toast('还没选中任何卡片');
  if (!await ask('删除选中的 ' + nMsg + ' 张（一起 ' + ms.length + ' 条）？\n'
      + '索引和文件一起删，删完找不回来。将释放 ' + size(nSize) + '。')) return;
  try {
    const r = await post('/api/delete', {chat_id: state.chat.chat_id, msg_ids: ms});
    if (r.error) return toast(r.error);
    toast('已删除 ' + r.deleted + ' 条，释放 ' + size(r.bytes));
    state.total = Math.max(0, state.total - r.deleted);
    paintCount();
    // 不退出选择模式（点「完成」才走）：只清掉已删卡片的选中态
    state.sel.clear();
    for (const f of document.querySelectorAll('#items figure.sel')) f.classList.remove('sel');
    paintPick();
    loadItems(true);
    pollStats();
    refreshAfterDelete();
  } catch (e) { toast('删除失败：' + e.message); }
};

async function doDelete(g) {
  const n = g.members.length;
  if (!await ask('删除' + (n > 1 ? '这 ' + n + ' 个' : '这条') + '？\n'
      + '索引和文件一起删，删完找不回来。将释放 ' + size(g.size) + '。')) return;
  try {
    const r = await post('/api/delete', {chat_id: state.chat.chat_id,
                                         msg_ids: g.members.map(m => m.msg_id)});
    if (r.error) return toast(r.error);
    const c = state.cards.get(g.key);
    if (c) { c.el.remove(); state.cards.delete(g.key); }
    state.total = Math.max(0, state.total - r.deleted);
    paintCount();
    showEmpty();
    pollStats();
    refreshAfterDelete();
    toast('已删除 ' + r.deleted + ' 条，释放 ' + size(r.bytes));
  } catch (e) { toast('删除失败：' + e.message); }
}

async function doStar(g, btn) {
  const on = !g.star;
  try {
    await post('/api/star', {chat_id: state.chat.chat_id,
                             msg_ids: g.members.map(m => m.msg_id), on});
    g.star = on;
    if (btn) {
      btn.textContent = on ? '★ 已收藏' : '☆ 收藏';
      btn.classList.toggle('on', on);
    }
    toast(on ? '已收藏' : '取消收藏');
    if (state.filter === 'star') loadItems(true);
  } catch (e) { toast('失败：' + e.message); }
}

function setAuthor(id) {
  state.author = String(id);
  $('#author').value = String(id);
  syncFilter();
  loadItems(true);
}

function setTopic(g) {                        // 话题可点筛选：和 setAuthor 同构
  const id = g.topic_id ? String(g.topic_id) : '';
  state.topic = id;
  $('#topic').value = id;                     // 空串 = 「全部话题」那个 value="" 选项
  syncFilter();
  loadItems(true);
}

function card(g) {
  const el = document.createElement('figure');
  el.__g = g;                                  // 灯箱翻页要按屏幕上的顺序找条目
  const grid = document.createElement('div');
  grid.className = 'grid n' + Math.min(g.members.length, 4);
  for (const m of g.members.slice(0, 4)) grid.appendChild(cell(m, g));
  el.appendChild(grid);

  const cap = document.createElement('figcaption');
  const c1 = document.createElement('div');
  c1.className = 'cap';
  c1.textContent = g.caption || '';
  if (g.caption) c1.onclick = () => {          // 超出 3 行才需要点开
    const full = c1.scrollHeight > c1.clientHeight + 2;
    if (full || c1.classList.contains('full')) c1.classList.toggle('full');
  };
  else c1.onclick = null;
  const c2 = document.createElement('div');
  c2.className = 'meta';
  const k = document.createElement('span');
  k.className = 'k ' + g.status;
  k.textContent = g.kind;
  c2.appendChild(k);
  c2.appendChild(document.createTextNode(
    when(g.date) + (g.members.length > 1 ? ' · 共 ' + g.members.length + ' 个' : '')));
  if (g.deleted) {
    const d = document.createElement('span');
    d.className = 'k bad';
    d.textContent = '已删除';
    c2.appendChild(d);
  } else if (g.edited) {
    const e = document.createElement('span');
    e.className = 'k click';
    e.title = '点开看改之前是什么样';
    e.textContent = '已编辑';
    e.onclick = ev => { ev.stopPropagation(); toggleBefore(e, g); };
    c2.appendChild(e);
  }
  const c3 = document.createElement('div');
  c3.className = 'meta';
  if (state.chat && state.chat.kind === '频道') {
    c3.append(document.createTextNode(size(g.size)));   // 频道没有「作者」，只留大小
  } else {
    const au = document.createElement('span');
    au.className = 'au';
    au.textContent = (g.author_name ? g.author_name + ' ' : '') + g.author_id;
    au.title = '只看这个作者';
    au.onclick = () => setAuthor(g.author_id);
    const tp = document.createElement('span');   // 话题也能点了：跟作者一个待遇
    tp.className = 'au';
    tp.textContent = (g.topic_title || (g.topic_id ? '话题 ' + g.topic_id : 'General'));
    tp.title = '只看这个话题';
    tp.onclick = ev => { ev.stopPropagation(); setTopic(g); };
    c3.append(au, document.createTextNode(' · '), tp,
      document.createTextNode(' · ' + size(g.size)));
  }
  const c4 = document.createElement('div');
  c4.className = 'meta';
  if (g.permalink) {
    const a = document.createElement('a');
    a.href = g.permalink;
    a.target = '_blank';
    a.textContent = '原消息';
    c4.appendChild(a);
  }
  if (g.dups && g.dups.length) {          // 同一个文件被重发：合并成一张卡，另一条的原消息也留着
    const d = document.createElement('a');
    d.href = g.dups[0].permalink || g.permalink;
    d.target = '_blank';
    d.className = 'bad';
    d.title = '同一个文件重发了 ' + g.dups.length + ' 次（' + g.dups.map(x => when(x.date)).join('、')
      + '），磁盘上只存一份';
    d.textContent = '重复 ×' + (g.dups.length + 1) + '（看另一条）';
    c4.appendChild(d);
  }
  if (g.status !== 'done') {
    const s = document.createElement('span');
    s.className = 'bad';
    s.textContent = statusText(g);
    c4.appendChild(s);
  }
  const acts = document.createElement('div');
  acts.className = 'meta acts';
  const bs = document.createElement('button');
  bs.className = 'mini ghost';
  bs.textContent = '分享';
  bs.onclick = e => { e.stopPropagation(); shareMenu(g, bs); };
  const bst = document.createElement('button');
  bst.className = 'mini ghost' + (g.star ? ' on' : '');
  bst.textContent = g.star ? '★ 已收藏' : '☆ 收藏';
  bst.onclick = e => { e.stopPropagation(); doStar(g, bst); };
  acts.append(bs, bst);
  const bd = document.createElement('button');
  bd.className = 'mini ghost';
  bd.textContent = '删除';
  bd.onclick = e => { e.stopPropagation(); doDelete(g); };
  acts.appendChild(bd);
  cap.append(c1, c2, c3, c4);
  cap.appendChild(acts);
  const loc = localActs(g);
  if (loc) cap.appendChild(loc);
  el.appendChild(cap);
  return el;
}

function statusText(g) {
  if (g.status === 'dup') return '和另一条重复，等它下完';
  return g.status + (g.note ? '：' + g.note : '');
}

/* 点「已编辑」就地展开。换过媒体的行：原图 → 每次编辑前 → 现在，每张都能点开进灯箱；
   只改了说明的行还是下面那两行文字。用展开而不是弹窗 —— 手机上也要能看，也不用离开卡片。 */
function toggleBefore(el, g) {
  const next = el.nextSibling;
  if (next && next.className === 'before') { next.remove(); return; }
  const box = document.createElement('div');
  box.className = 'before';
  const m = g.members.find(x => x.revs) || g.members[0];
  if (m.revs) {
    const strip = document.createElement('div');
    strip.className = 'revstrip';
    const shots = m.revs.map((r, i) =>
      [(i === 0 ? '原图' : '第 ' + (i + 1) + ' 次编辑前'), r])
      .concat(m.now ? [['现在', m.now]] : []);
    for (const [label, s] of shots) {
      const cell = document.createElement('div');
      cell.className = 'revcell';
      const thumb = document.createElement('div');
      thumb.className = 'revthumb';
      const im = document.createElement('img');
      im.loading = 'lazy';
      im.src = mediaUrl(s.thumb || s.file);
      thumb.appendChild(im);
      if (s.kind === 'video') {                  // 视频也给张封面，别只有一张糊图
        const p = document.createElement('span');
        p.className = 'playmark';
        p.textContent = '▶';
        thumb.appendChild(p);
      }
      thumb.onclick = e => { e.stopPropagation(); light({key: 'rev', members: [s]}, s); };
      const cap = document.createElement('div');
      cap.className = 'muted';
      cap.textContent = label;
      const isNow = s.seq === undefined;
      const del = document.createElement('button');
      del.className = 'revdel';
      del.textContent = '删';
      del.onclick = async e => {
        e.stopPropagation();
        if (!await ask(`把「${label}」这版从归档里删掉？只删这一版，其余版本都保留，Telegram 不动。`, '删除')) return;
        const body = {chat_id: state.chat.chat_id, msg_id: m.msg_id};
        if (isNow) body.current = true; else body.rev_seq = s.seq;
        const r = await post('/api/delete', body);
        box.remove();
        if (isNow && r.deleted) {              // 历史也空了：这条没了，整卡消失
          const c = state.cards.get(g.key);
          if (c) { c.el.remove(); state.cards.delete(g.key); }
          state.total = Math.max(0, state.total - 1);
          paintCount();
          showEmpty();
        } else if (isNow) {
          await loadItems(true);               // 回退一版：重画卡片，新「现在」顶上来
        }
        toast(r.bytes ? '已删，释放 ' + Math.round(r.bytes / 1024) + ' KB' : '已删');
        refreshAfterDelete();
      };
      cap.appendChild(del);
      cell.append(thumb, cap);
      strip.appendChild(cell);
    }
    box.appendChild(strip);
  }
  const a = document.createElement('div');
  a.textContent = '改前：' + (g.caption_before || '（原来没有说明）');
  const b = document.createElement('div');
  b.textContent = '现在：' + (g.caption || '（没有说明）');
  box.append(a, b);
  el.after(box);
}

/* 本机才有的两项：在资源管理器里定位、用默认播放器打开。
   没下好的卡片也显示，只是灰着并写明原因 —— 免得找不到按钮，以为没做。 */
function localActs(g) {
  if (!(state.status && state.status.local)) return null;
  const abs = memberAbs(g);
  const row = document.createElement('div');
  row.className = 'meta acts';
  const mk = (label, path) => {
    const b = document.createElement('button');
    b.className = 'mini ghost';
    b.textContent = label;
    b.disabled = !abs;
    b.title = abs ? '' : '还没下好，磁盘上还没有文件';
    b.onclick = e => { e.stopPropagation(); reveal(g, path); };
    return b;
  };
  row.append(mk('打开文件夹', '/api/reveal'), mk('本地打开', '/api/open'));
  return row;
}

// 卡片指纹：变了才重画。说明文字和编辑/删除标记也算在内 —— 编辑过的卡片要原地刷新，
// 而不是等用户手动刷新页面。
const sig = g => [g.kind, g.status, g.note || '', g.members.length, g.caption || '',
                  g.caption_before || '', g.edited || 0, g.deleted ? 1 : 0, g.star ? 1 : 0,
                  (g.dups || []).length,
                  g.members.map(m => (m.thumb ? 1 : 0) + (m.file ? 1 : 0)).join('')].join('|');

function upsert(groups, prependNew) {
  const box = $('#items');
  let added = 0;
  // 插到顶上的时候要**倒着**走：prepend 每次都插到最前面，正着走会把同批新来的
  // 几条整个倒过来（后到的压在先到的上面）。正序是给翻页用的 appendChild。
  for (const g of (prependNew ? groups.slice().reverse() : groups)) {
    const s = sig(g);
    const old = state.cards.get(g.key);
    if (old && old.sig === s) continue;
    const el = card(g);
    if (state.pick && state.sel.has(g.key)) el.classList.add('sel');  // 原地替换时把选中态贴回来
    if (old) old.el.replaceWith(el);
    else if (prependNew) { box.prepend(el); added++; }
    else { box.appendChild(el); added++; }
    state.cards.set(g.key, {el: el, sig: s});
  }
  paintCount();
  showEmpty();
  paintProgress();
  fitMasonry();          // 同步量高度：rAF 在后台/被遮住的标签页里根本不触发，高度就白算了
  return added;
}

function showEmpty() {
  const e = $('#empty');
  const none = $('#items').querySelectorAll('figure').length === 0;   // 哨兵不算卡片
  e.classList.toggle('hide', !none);
  if (!none) return;
  e.innerHTML = '<div class="emptybox">' + (state.chat
    ? '<b>还没有内容。</b><br>开启监听之后，这个群里符合类型的新消息会自动出现在这里。<br>' +
      '要看更早的，用上面的「补历史」。'
    : '左边选一个群或频道开始。') + '</div>';
}

/* ---------- 统计：最大文件 + 按作者占用 ---------- */
async function loadStats() {
  if (!state.chat) return;
  $('#items').innerHTML = '';
  state.cards.clear();
  $('#more').classList.add('hide');
  state.more = false;
  $('#fhint').textContent = '';
  try {
    const cid = state.chat.chat_id;
    const [top, au] = await Promise.all([
      api('/api/top?chat=' + cid + '&limit=200'),
      api('/api/authors?chat=' + cid)]);
    const box = $('#items');
    box.className = 'items stats';
    box.innerHTML = '';

    const h1 = document.createElement('h3');
    h1.textContent = '最大的文件（点删除会连文件一起删）';
    box.appendChild(h1);
    const t1 = document.createElement('table');
    t1.className = 'stat';
    for (const it of top.items || []) {
      const tr = document.createElement('tr');
      const td1 = document.createElement('td');
      td1.className = 'num';
      td1.textContent = size(it.size);
      const td2 = document.createElement('td');
      td2.textContent = (it.kind === 'video' ? '视频' : '图片') + ' · '
        + when(it.date) + ' · ' + (it.author_name || it.author_id) + ' · '
        + (it.caption || '').slice(0, 40);
      const td3 = document.createElement('td');
      const b = document.createElement('button');
      b.className = 'mini ghost';
      b.textContent = '删除';
      b.onclick = () => doDelete({key: 'msg:' + cid + ':' + it.msg_id, members: [it]});
      td3.appendChild(b);
      tr.append(td1, td2, td3);
      t1.appendChild(tr);
    }
    box.appendChild(t1);

    const h2 = document.createElement('h3');
    h2.textContent = '按作者占用（按 sha 去重，点作者即筛选）';
    box.appendChild(h2);
    const t2 = document.createElement('table');
    t2.className = 'stat';
    for (const a of au.authors || []) {
      const tr = document.createElement('tr');
      const td1 = document.createElement('td');
      td1.className = 'num';
      td1.textContent = a.bytes ? size(a.bytes) : '—';
      const td2 = document.createElement('td');
      const link = document.createElement('span');
      link.className = 'au';
      link.textContent = (a.name || a.author_id) + '（' + a.n + ' 条）';
      link.onclick = () => setAuthor(a.author_id);
      td2.appendChild(link);
      tr.append(td1, td2);
      t2.appendChild(tr);
    }
    box.appendChild(t2);
    $('#count').textContent = (top.items || []).length + ' 条在榜上';
  } catch (e) { toast('统计失败：' + e.message); }
}

/* 列表查询参数。loadItems / pollLive / 导出 / 打包全用这一个 ——
   以前 pollLive 自己拼了一份不带筛选的，每 2.5 秒把最新 60 条塞回来，筛选看着就像没生效；
   导出和打包又各拼一份，结果「屏幕上筛出来的」和「导出去的」对不上。 */
function itemsQs(limit) {
  const qs = new URLSearchParams({chat: state.chat.chat_id});
  if (limit) qs.set('limit', limit);
  if (state.filter === 'photo' || state.filter === 'video') qs.set('kinds', state.filter);
  else if (state.filter) qs.set('status', state.filter);
  if (state.author) qs.set('author', state.author);
  if (state.topic) qs.set('topic', state.topic);
  return qs;
}

/* ---------- 滚到底自动加载 ---------- */
/* 标准做法：列表末尾放一个**真哨兵元素**，它进视野就再拉一页。
   盯「最后一张卡」不行：卡片会被原地替换，页面比屏幕短时也不会二次触发，就卡住了。
   三个触发源兜底：哨兵相交 / scroll 位置 / 每页加载完自己再看一眼。 */
let io = null, sentEl = null;
if (window.IntersectionObserver) {
  io = new IntersectionObserver(es => { if (es.some(e => e.isIntersecting)) maybeMore(); },
                                {root: $('#items'), rootMargin: '600px'});
}
function mountSent() {
  const box = $('#items');
  if (!sentEl) {
    sentEl = document.createElement('div');
    sentEl.id = 'sent';
  }
  if (box.lastElementChild !== sentEl) box.appendChild(sentEl);
  if (io) {          // 重新武装：页面太短时 IO 不会自己再触发，重新 observe 一次才接得上
    io.disconnect();
    io.observe(sentEl);
  }
}
/* 「还有更老的」单独一个标志位，不靠 #more 显不显示来判断 ——
   那一条只在真加载时出现，空闲时藏着，不然它一直写着「正在加载…」就是句假话。 */
const moreOk = () => !state.loading && state.more;
function maybeMore() {
  if (!state.chat || state.tab !== 'list' || !moreOk()) return;
  const b = $('#items');
  if (b.scrollTop + b.clientHeight >= b.scrollHeight - 600) loadItems(false);
}
$('#items').addEventListener('scroll', maybeMore);

/* 列表底下那一条：只在加载中 / 出错时出现；到底了和空闲时都收起来。
   不再有「加载更多」按钮，翻页是自动的（还有多少看顶部那个「已显示 M / 共 N 条」）。 */
function showMore(text, retry) {
  const box = $('#more');
  box.innerHTML = '';
  if (retry) {
    const b = document.createElement('button');
    b.className = 'ghost';
    b.textContent = '重试';
    b.onclick = () => loadItems(false);
    box.appendChild(b);
  }
  const s = document.createElement('span');
  s.className = 'muted small';
  s.textContent = text;
  box.appendChild(s);
  box.classList.remove('hide');
}

/* 顶部条数：共 N 条 = 当前筛选下的**总数**，不只这一页。
   导出/打包的也是这些，所以顺手让人知道会导出多少。 */
function paintCount() {
  const shown = $('#items').querySelectorAll('figure').length;
  if (!state.total) {
    $('#count').textContent = shown ? shown + ' 条' : '';
    return;
  }
  $('#count').textContent = shown >= state.total
    ? '共 ' + state.total + ' 条'
    : '已显示 ' + shown + ' / 共 ' + state.total + ' 条';
}

async function loadItems(reset) {
  if (!state.chat || state.loading) return;
  state.loading = true;
  $('#items').classList.remove('stats');     // 从统计页切回来时把布局类还原（整体换 className 会把编辑模式的 pick 一起抹掉）
  const limit = 60;
  const qs = itemsQs(limit);
  if (!reset && state.minMsg) qs.set('before', state.minMsg);
  if (!reset) showMore('正在加载…');
  try {
    const r = await api('/api/items?' + qs);
    const groups = r.groups || [];
    if (reset) {
      $('#items').innerHTML = '';
      state.cards.clear();
      state.minMsg = null;
      if (state.pick) setPick(true);   // 重拉 = 换了一批卡片：选中清零，残留 key 不留（计数才等于实际能删的）
    }
    state.total = r.total || 0;
    upsert(groups, false);
    // 翻页游标用服务端给的最小消息号（卡片自己的成员里可能比它大，
    // 拿成员的当游标会让下一页少取几条）。
    if (r.min_msg != null) state.minMsg = r.min_msg;
    // 「还有没有更老的」看这一页取到了几行（去重前的行数），不是出了几张卡片：
    // 有重复时 60 行可能只出 40 多张卡，按卡片数判断会提前停、后面永远翻不到。
    state.more = (r.rows || 0) >= limit;
    $('#more').classList.add('hide');
  } catch (e) {
    state.more = false;
    $('#count').textContent = '加载失败：' + e.message;
    showMore('加载失败：' + e.message, true);
  } finally {
    state.loading = false;
    mountSent();
    maybeMore();          // 第一页就没填满屏幕时，靠这一下接着拉
  }
}

async function pollLive() {
  if (!state.chat || state.tab !== 'list' || !state.up) return;
  try {
    const r = await api('/api/items?' + itemsQs(60));
    const box = $('#items');
    const before = box.scrollHeight, top = box.scrollTop;
    if (r.total) state.total = r.total;          // 新消息进来了，顶部那个「共 N 条」也要跟着长
    if (upsert(r.groups || [], true)) box.scrollTop = top + (box.scrollHeight - before);
  } catch (e) { /* 下一轮再说 */ }
}

async function pollStats() {
  try {
    const s = await api('/api/stats');
    state.stats = s;
    let t = '已存 ' + gb(s.used) + ' / 上限 ' + gb(s.cap);
    if (s.over_cap) t += '（到上限，只记索引）';
    $('#stats').textContent = t;
  } catch (e) { /* 忽略 */ }
}

/* ---------- 大图 / 播放：支持左右翻页 ---------- */
function viewList() {
  // 按屏幕上的显示顺序收集所有能看的条目（相册里每条算一个）
  const out = [];
  for (const el of document.querySelectorAll('#items > figure')) {
    const g = el.__g;
    if (g) for (const m of g.members) out.push([g, m]);
  }
  return out;
}

function light(g, m) {
  const list = viewList();
  let i = list.findIndex(([gg, mm]) => gg === g && mm === m);
  if (i < 0) { list.unshift([g, m]); i = 0; }
  state.lb = {list, i};
  drawLight();
}

function drawLight() {
  const lb = state.lb;
  if (!lb) return;
  const [g, m] = lb.list[lb.i];
  const box = $('#lightbox');
  const old = box.querySelector('.lbnav');
  if (old) old.remove();
  const olddel = document.getElementById('lbdel');   // 顶右「删除此媒体」
  if (olddel) olddel.remove();
  box.innerHTML = '';
  const mid = document.createElement('div');   // 媒体夹在两个按钮中间：按钮贴着图/视频，不贴屏幕边
  mid.className = 'mid';
  const wrap = document.createElement('div');  // 删除钮贴媒体右上角，要相对媒体定位
  wrap.className = 'lbwrap';
  if (m.kind === 'video' && m.file) {
    const v = document.createElement('video');
    v.controls = true;
    v.autoplay = true;
    v.preload = 'metadata';
    v.src = mediaUrl(m.file);
    if (m.thumb) v.poster = mediaUrl(m.thumb);
    wrap.appendChild(v);
  } else {
    const im = document.createElement('img');
    im.src = mediaUrl(m.file || m.thumb);
    wrap.appendChild(im);
    if (m.kind === 'video') {                  // 还没下好的视频：别再拿缩略图当视频源
      const n = document.createElement('div');
      n.className = 'lbnote';
      n.textContent = '还没下好，先看缩略图';
      mid.appendChild(n);
    }
  }
  mid.appendChild(wrap);
  const nav = document.createElement('div');
  nav.className = 'lbnav';
  const prev = document.createElement('button');
  prev.textContent = '‹';
  prev.title = '上一条（←）';
  prev.onclick = e => { e.stopPropagation(); stepLight(-1); };
  const info = document.createElement('span');   // 顶部居中胶囊（样式 fixed，放哪都行）
  info.textContent = (lb.i + 1) + ' / ' + lb.list.length
    + (m.kind === 'video' ? ' · 视频' : '') + (g.members.length > 1 ? ' · 相册' : '');
  const next = document.createElement('button');
  next.textContent = '›';
  next.title = '下一条（→）';
  next.onclick = e => { e.stopPropagation(); stepLight(1); };
  nav.append(prev, mid, next);
  box.appendChild(nav);
  const del = document.createElement('button');   // 相册里一次删一张：删的就是当前这条
  del.id = 'lbdel';
  del.className = 'lbdel';
  del.textContent = '删除';
  del.title = '删除这条媒体（索引和文件一起删）';
  del.onclick = e => { e.stopPropagation(); delLight(g, m); };
  wrap.appendChild(del);
  $('#light').classList.remove('hide');
}

async function delLight(g, m) {
  if (!await ask('删除这条？\n索引和文件一起删，删完找不回来。'
                 + '将释放 ' + size(m.size || 0) + '。')) return;
  try {
    const r = await post('/api/delete', {chat_id: state.chat.chat_id,
                                         msg_ids: [m.msg_id]});
    if (r.error) return toast(r.error);
    const lb = state.lb;
    lb.list = lb.list.filter(([, mm]) => mm.msg_id !== m.msg_id);
    g.members = g.members.filter(x => x.msg_id !== m.msg_id);
    lb.i = Math.min(lb.i, lb.list.length - 1);
    if (lb.list.length) drawLight();          // 相册里还有下一张：接着看
    else closeLight();
    state.total = Math.max(0, state.total - r.deleted);
    paintCount(); showEmpty(); pollStats(); refreshAfterDelete();
    loadItems(true);                          // 卡片重画（少了一个格子）
    toast('已删除这条媒体，释放 ' + size(r.bytes));
  } catch (e) { toast('删除失败：' + e.message); }
}

function stepLight(d) {
  const lb = state.lb;
  if (!lb || !lb.list.length) return;
  if (d > 0 && lb.i === lb.list.length - 1 && state.more) {
    // 翻到最后一条、下一页还没拉回来：先拉回来再接着翻，不用退出灯箱去点「更多」
    loadItems(false).then(() => {
      const list = viewList();
      if (list.length > lb.list.length) { lb.list = list; lb.i++; drawLight(); }
    });
    return;
  }
  lb.i = (lb.i + d + lb.list.length) % lb.list.length;
  drawLight();
}

const closeLight = () => {                  // 关灯箱时把顶右删除按钮一起摘掉，先把还在播的视频停掉
  const v = document.querySelector('#lightbox video');
  if (v) v.pause();
  $('#light').classList.add('hide');
  const d = document.getElementById('lbdel');
  if (d) d.remove();
};
$('#light').onclick = closeLight;
$('#lightbox').onclick = e => e.stopPropagation();
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeLight(); $('#drawer').classList.add('hide'); }
  if ($('#light').classList.contains('hide')) return;
  if (e.key === 'ArrowLeft') stepLight(-1);
  if (e.key === 'ArrowRight') stepLight(1);
});

/* 桌面：滚轮也翻上一张/下一张（手机触屏没有滚轮，不受影响）。
   150ms 节流：滚轮一格会吐好几个事件，不拦一下就一下翻过好几张。 */
let lbWheelAt = 0;
$('#light').addEventListener('wheel', e => {
  e.preventDefault();
  const now = Date.now();
  if (now - lbWheelAt < 150) return;
  lbWheelAt = now;
  stepLight(e.deltaY > 0 ? 1 : -1);
}, {passive: false});

/* 设置抽屉里「手机怎么连」那一块：只在设了新密码时回显一次。 */
function drawLan() {
  $('#lanpw').value = '';
  $('#lanstate').textContent = state.newPw
    ? '访问密码（只显示这一次，记下来）：' + state.newPw
    : '';
}

/* 启动口令 / 明文残留文件这两块。只说真话：两档各挡得住什么、代价是什么。 */
function drawLock(s) {
  $('#dlockpw').value = '';
  $('#lockoff').classList.toggle('hide', !s.passcode_set);
  $('#lockstate').textContent = s.passcode_set
    ? '已设启动口令：每次重启服务都要在这台电脑上输一次。口令没存过，忘了只能重置重登。'
    : '没设启动口令：凭证只用 Windows 账户加密（DPAPI），重启自动解开 —— 挡得住复制文件的木马，'
      + '挡不住同账户里主动调 DPAPI 的木马。设口令就多一把锁，代价是每次重启输一次。';
  $('#legacywarn').textContent = s.legacy_file
    ? '磁盘上还留着明文的 archive.session（已经不用它了），建议删掉。'
    : '';
}

/* ---------- 设置抽屉 ---------- */
$('#settings').onclick = () => {
  const s = state.status || {};
  drawLan();
  drawLock(s);
  $('#did').value = s.api_id || '';
  $('#dhash').value = '';
  $('#dhash').placeholder = s.api_hash_set ? '已设置，留空表示不改' : '留空（重新登录时才需要）';
  $('#dcap').value = state.stats ? Math.round(state.stats.cap / (1 << 30)) : 50;
  $('#dstate').textContent = '';
  $('#drawer').classList.remove('hide');
};
$('#dclose').onclick = () => $('#drawer').classList.add('hide');
$('#drawer').onclick = e => { if (e.target.id === 'drawer') $('#drawer').classList.add('hide'); };
$('#sclose').onclick = () => $('.side').classList.remove('open');

$('#dsave').onclick = async () => {
  const s = state.status || {};
  const id = $('#did').value.trim(), hash = $('#dhash').value.trim();
  const bad = checkCreds(id || s.api_id, hash);
  if (bad) return toast(bad);
  $('#dsave').disabled = true;
  try {
    const newlock = $('#dlockpw').value;
    if (newlock) {                              // 设口令：服务端只认本机请求
      const r = await post('/api/passcode', {passcode: newlock});
      if (!r.ok) { $('#dstate').textContent = '口令没设上：' + r.error; return; }
    }
    const pw = $('#lanpw').value;
    const r = await post('/api/settings', {disk_cap_gb: +$('#dcap').value || 50,
                                           lan_password: pw || ''});
    if ((id && String(id) !== String(s.api_id || '')) || hash) {
      applyStatus(await post('/api/config', {api_id: id || s.api_id, api_hash: hash}));
    }
    toast(newlock ? '已保存（启动口令已设，下次重启要在本机输一次）'
                  : pw ? '已保存（访问密码已更新）' : '已保存');
    state.newPw = null;
    drawLan();
    drawLock({...state.status, passcode_set: !!newlock || s.passcode_set});
    $('#drawer').classList.add('hide');
    pollStats();
  } catch (e) { $('#dstate').textContent = '保存失败：' + e.message; }
  finally { $('#dsave').disabled = false; }
};

$('#lockoff').onclick = async () => {
  try {
    const r = await post('/api/passcode', {passcode: ''});
    if (!r.ok) return toast(r.error);
    toast('已取消启动口令，以后重启会自动解开');
    drawLock({...state.status, passcode_set: false});
  } catch (e) { toast('取消失败：' + e.message); }
};

/* ---------- 交互 ---------- */
document.querySelectorAll('.tabs button').forEach(b => {
  b.onclick = async () => {
    document.querySelectorAll('.tabs button').forEach(x => x.classList.remove('on'));
    b.classList.add('on');
    state.tab = b.dataset.tab;
    setPick(false);                                        // 切页不带着选择过去
    $('#editbtn').classList.toggle('hide', state.tab === 'stats');  // 统计页没有卡片可选
    if (state.tab === 'stats') return loadStats();
    await loadItems(true);
  };
});
const FILTER_HINT = {
  skipped: '「跳过」= 文件超过单文件上限，磁盘上没有文件，只留了索引；点「原消息」去 Telegram 看',
  deleted: '「已删除」= 消息在 Telegram 里已经被删掉了，但归档还留着（文件也在磁盘上）。'
    + '复查完可以用「本群设置」里的「清理已删除」一次清掉',
  edited: '「已编辑」= 作者换过媒体或改过说明的；点卡片上的「已编辑」能展开原图到现在的每一版。'
    + '每版都能单独删；删「现在」= 回退到上一版（大图还在），删到一版不剩才整条消失；都只动归档，Telegram 不动',
};
const FILTER_LABEL = {photo: '只看图片', video: '只看视频', done: '只看下好的',
                      pending: '只看等待中', skipped: '只看跳过的',
                      deleted: '只看已删除', star: '只看收藏', edited: '只看已编辑'};

/* 筛选按钮上写着当前筛的是什么 —— 不然筛完就忘了，只会觉得「东西怎么少了」 */
function syncFilter() {
  const bits = [];
  if (state.filter) bits.push(FILTER_LABEL[state.filter] || state.filter);
  if (state.author) bits.push('作者');
  if (state.topic) bits.push('话题');
  const b = $('#fbtn');
  b.textContent = bits.length ? '筛选 · ' + bits.join(' + ') : '筛选';
  b.classList.toggle('on', bits.length > 0);
}

/* 收着的下拉上滚滚轮直接换选项，不用点开：滚到头绕回第一个（wheel 在收着的 select 上
   原生是不换值的，得自己来）。惯性滚动一口气发一大串 wheel，250ms 一格，别每格都拉列表；
   改值后照常走各自的 change 逻辑（state/筛选按钮文字/拉列表都在那边） */
let wheelAt = 0;
for (const id of ['#filter', '#author', '#topic']) {
  $(id).addEventListener('wheel', e => {
    e.preventDefault();                                  // 别让页面跟着滚
    const now = Date.now();
    if (now - wheelAt < 250) return;
    wheelAt = now;
    const sel = $(id);
    if (sel.options.length < 2) return;
    const d = e.deltaY > 0 ? 1 : -1;
    const from = sel.selectedIndex;
    sel.selectedIndex = (from + d + sel.options.length) % sel.options.length;
    sel.dispatchEvent(new Event('change'));
  }, {passive: false});
}

/* 「本群设置」和「筛选」两个面板同时只开一个，点别处也收起来 */
function closePanels() {
  $('#cfg').classList.remove('open');
  $('#fpanel').classList.remove('open');
}
$('#cfgbtn').onclick = e => {
  e.stopPropagation();
  const on = !$('#cfg').classList.contains('open');
  closePanels();
  $('#cfg').classList.toggle('open', on);
};
$('#fbtn').onclick = e => {
  e.stopPropagation();
  const on = !$('#fpanel').classList.contains('open');
  closePanels();
  $('#fpanel').classList.toggle('open', on);
};
document.addEventListener('click', e => {
  if (!e.target.closest('#cfg, #fpanel, #cfgbtn, #fbtn, #ask, #items, #light, #pickbar, #editbtn'))
    closePanels();
});

/* 换开筛选后重灌两个下拉：0 条的（删空的、除正选的那一个）此刻从下拉里消失 */
function pruneZero() {
  if (!state.chat) return;
  loadAuthors(state.chat);
  loadTopics(state.chat);
}

$('#filter').onchange = () => {
  state.filter = $('#filter').value;
  $('#fhint').textContent = FILTER_HINT[state.filter] || '';
  syncFilter();
  loadItems(true);
  pruneZero();
};
$('#author').onchange = () => {
  state.author = $('#author').value;
  syncFilter();
  loadItems(true);
  pruneZero();
};
$('#topic').onchange = () => {
  state.topic = $('#topic').value;
  syncFilter();
  loadItems(true);
  pruneZero();
};

/* 导出和打包跟着当前筛选走（服务端用的是同一套条件），条数就是顶部那个「共 N 条」 */
$('#csvbtn').onclick = () => {
  if (!state.chat) return toast('先选一个群');
  toast('正在导出' + (state.total ? '当前筛选的 ' + state.total + ' 条' : '清单') + '…');
  location.href = '/api/export.csv?' + itemsQs();
};
$('#zipbtn').onclick = async () => {
  if (!state.chat) return toast('先选一个群');
  $('#zipbtn').disabled = true;
  toast('正在打包' + (state.total ? '当前筛选的 ' + state.total + ' 条' : '')
        + '，文件多的话要等一会儿…');
  try {
    const r = await post('/api/zip?' + itemsQs(), {});
    if (r.error) return toast(r.error);
    toast('打包好了：' + r.n + ' 个文件'
      + (r.hist ? '（另含 ' + r.hist + ' 个历史版本）' : '') + '，' + size(r.size));
    revealPath(r.file);
  } catch (e) { toast('打包失败：' + e.message); }
  finally { $('#zipbtn').disabled = false; }
};

async function revealPath(full) {
  if (!full || !(state.status && state.status.local)) return;
  try { await post('/api/reveal', {path: full}); } catch (e) { /* 忽略 */ }
}

$('#purge').onclick = async () => {
  if (!state.chat) return;
  if (!await ask('把本群所有「跳过」条目删掉？\n'
      + '它们只有索引没有文件（超过单文件上限的那些），删掉列表会干净些，'
      + '但原消息链接也就一起没了。')) return;
  try {
    const r = await post('/api/delete', {chat_id: state.chat.chat_id, status: 'skipped'});
    toast('清理了 ' + r.deleted + ' 条');
    loadItems(true);
    pollStats();
    refreshAfterDelete();
  } catch (e) { toast('清理失败：' + e.message); }
};

$('#purgedel').onclick = async () => {
  if (!state.chat) return;
  if (!await ask('把本群所有「Telegram 上已删除」的条目连文件一起清理掉？')) return;
  try {
    const r = await post('/api/delete', {chat_id: state.chat.chat_id, deleted: true});
    toast('清理了 ' + r.deleted + ' 条，释放 ' + size(r.bytes));
    loadItems(true);
    pollStats();
    refreshAfterDelete();
  } catch (e) { toast('清理失败：' + e.message); }
};

/* 桌面端左边的群列表也能收起来，状态记着。
   窄屏上这个按钮不显示（群列表在底部标签栏上）。 */
function applySide() {
  $('#app').classList.toggle('noside', localStorage.getItem('arc.side') === 'off');
}
$('#sidebtn').onclick = () => {
  localStorage.setItem('arc.side', localStorage.getItem('arc.side') === 'off' ? 'on' : 'off');
  applySide();
  fitMasonry();          // 宽度变了，卡片高度要重算
};
applySide();

/* 窄屏底部标签栏：借用上面那几个按钮自己的处理逻辑，不重复实现一遍 */
for (const b of document.querySelectorAll('#tabbar button')) {
  b.onclick = () => {
    const k = b.dataset.mob;
    if (k === 'chats') return $('.side').classList.toggle('open');
    if (k === 'settings') return $('#settings').click();
    for (const x of document.querySelectorAll('#tabbar button')) x.classList.toggle('on', x === b);
    const tab = document.querySelector('#app [data-tab="' + k + '"]');
    if (tab) tab.click();
  };
}
$('#search').oninput = renderChats;
$('#refresh').onclick = () => loadChats(true);

tick();
setInterval(tick, 2500);
