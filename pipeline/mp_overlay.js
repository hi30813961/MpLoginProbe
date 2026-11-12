/* 公众号后台注入式增强面板 v4 —— 页面感知双视图。
   素材库(filepage) / 草稿箱(appmsg list_card) 各有自己的主题、信息区、按钮标签与统计。
   通过 CDP Runtime.evaluate 注入，只追加 DOM，不修改/拦截官方元素与请求。
   与本机桥通信靠 window.__mpui.take()/render()/onResult()，页面内不发起外部请求。

   v3 已解决的问题继续保留：按钮常驻 dock 且只创建一次 + 事件委托、
   数据签名无变化不重绘、保存/恢复滚动位置、二次确认按钮内可视化、拖动/双击复位。
   v4 新增：页签(本机/当前页/输出)、搜索与状态筛选、Toast、快捷键、位置记忆、
   宽窄切换、按页面定制的当前页信息卡、官方草稿缓存展示、主题色随页面切换。
*/
(function () {
  var V = 4;
  if (window.__mpuiTimer) { try { clearInterval(window.__mpuiTimer); } catch (e) {} }
  if (window.__mpui && window.__mpui.v === V) return;

  var HOST_ID = 'mpui-host-v4';
  var BTN_ID = 'mpui-toolbar-btn';
  ['mpui-host-v4', 'mpui-host-v3', 'mpui-host-v2'].forEach(function (id) {
    var x = document.getElementById(id); if (x) x.remove();
  });
  var oldBtn = document.getElementById(BTN_ID);
  if (oldBtn) oldBtn.remove();

  function el(tag, css, html) {
    var e = document.createElement(tag);
    if (css) e.style.cssText = css;
    if (html != null) e.innerHTML = html;
    return e;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // ── 页面识别与主题 ────────────────────────────────────────────────────
  function routeOf() {
    var u = location.href;
    if (/appmsg_edit|action=edit/.test(u)) return 'editor';
    if (/filepage/.test(u)) return 'material';
    if (/appmsgpublish/.test(u)) return 'published';
    if (/\/cgi-bin\/appmsg\?/.test(u) && /action=list|list_card/.test(u)) return 'draft';
    return 'other';
  }
  var R = routeOf();
  var THEMES = {
    material:  { name: '素材库',   icon: '▦', color: '#07c160', dark: '#059a4c',
                 sub: '官方素材 · 本地流水线' },
    draft:     { name: '草稿箱',   icon: '✎', color: '#576b95', dark: '#455780',
                 sub: '草稿闭环 · 本地流水线' },
    published: { name: '发表记录', icon: '◉', color: '#8b5cf6', dark: '#7143d4',
                 sub: '发布台账 · 本地流水线' },
    other:     { name: '公众号后台', icon: '●', color: '#4b5563', dark: '#374151',
                 sub: '本地素材流水线' },
    editor:    { name: '图文编辑器', icon: '✎', color: '#4b5563', dark: '#374151', sub: '' }
  };
  var TH = THEMES[R] || THEMES.other;

  var C = {
    panel: 'position:fixed;right:16px;top:56px;width:340px;' +
           'height:calc(100vh - 68px);max-height:560px;' +
           'display:flex;flex-direction:column;background:#fff;border:1px solid #e3e6eb;' +
           'border-radius:11px;box-shadow:0 10px 34px rgba(20,30,40,.19);' +
           'font:12px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;' +
           'color:#353535;z-index:2147483000;overflow:hidden',
    head: 'display:flex;align-items:center;gap:7px;padding:9px 10px;' +
          'color:#fff;flex:none;cursor:move;user-select:none',
    badge: 'font-size:15px;line-height:1;opacity:.9',
    title: 'font-size:13px;font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis',
    sub: 'font-size:10px;opacity:.82;font-weight:400;display:block;margin-top:1px',
    icon: 'width:21px;height:21px;border:0;border-radius:5px;background:rgba(255,255,255,.18);' +
          'color:#fff;cursor:pointer;font-size:12px;line-height:1;flex:none',
    status: 'padding:6px 10px;background:#f7f8fa;border-bottom:1px solid #eceef1;' +
            'font-size:11px;color:#5b6470;flex:none;white-space:nowrap;' +
            'overflow:hidden;text-overflow:ellipsis;cursor:pointer',
    tabs: 'display:flex;gap:0;padding:0 8px;border-bottom:1px solid #eceef1;' +
          'background:#fff;flex:none',
    tab: 'padding:7px 12px 6px;border:0;border-bottom:2px solid transparent;' +
         'background:transparent;color:#8b929c;font-size:11px;cursor:pointer;white-space:nowrap',
    view: 'padding:10px 12px;overflow:auto;flex:1;min-height:0',
    dock: 'padding:7px 10px 9px;border-top:1px solid #e8eaee;background:#fcfcfd;flex:none',
    picked: 'font-size:10px;color:#8b929c;margin:0 0 5px;white-space:nowrap;' +
            'overflow:hidden;text-overflow:ellipsis',
    btn: 'padding:4px 7px;border:1px solid #dfe3e8;border-radius:5px;background:#fff;' +
         'color:#353535;font-size:10px;cursor:pointer;margin:0 4px 4px 0;white-space:nowrap',
    btnWarn: 'padding:4px 7px;border:1px solid #f0a020;border-radius:5px;background:#fff5e2;' +
             'color:#a9760b;font-size:10px;font-weight:600;cursor:pointer;' +
             'margin:0 4px 4px 0;white-space:nowrap',
    search: 'width:100%;box-sizing:border-box;padding:6px 9px;margin-bottom:7px;' +
            'border:1px solid #e3e6eb;border-radius:6px;font-size:11px;outline:none;background:#fafbfc',
    chips: 'display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px',
    chip: 'padding:2px 8px;border-radius:10px;border:1px solid #e6e9ee;background:#fff;' +
          'color:#6b7280;font-size:10px;cursor:pointer',
    card: 'border:1px solid #eceef1;border-radius:8px;padding:7px 9px;margin-bottom:6px;cursor:pointer;' +
          'transition:border-color .15s,background .15s',
    name: 'font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis',
    sub2: 'font-size:10px;color:#9aa0a8;margin-top:3px',
    h: 'font-size:11px;color:#8b929c;margin:2px 0 7px;font-weight:600',
    stat: 'display:flex;gap:5px;flex-wrap:wrap;margin-bottom:9px',
    pill: 'padding:2px 7px;border-radius:9px;background:#f2f4f6;color:#6b7280;font-size:10px',
    pillOk: 'padding:2px 7px;border-radius:9px;background:#e9f8ef;color:#0a7a42;font-size:10px',
    pillWarn: 'padding:2px 7px;border-radius:9px;background:#fdf4e3;color:#a9760b;font-size:10px',
    big: 'border:1px solid #eceef1;border-radius:8px;padding:10px 12px;margin-bottom:9px;' +
         'background:linear-gradient(135deg,#fafbfc,#fff)',
    bigNum: 'font-size:22px;font-weight:700;line-height:1.2',
    bigLabel: 'font-size:10px;color:#8b929c;margin-left:6px',
    row: 'font-size:10px;color:#6b7280;word-break:break-all;margin-bottom:3px;' +
         'padding:4px 6px;background:#fafbfc;border-radius:4px',
    log: 'background:#f7f8fa;border:1px solid #eceef1;border-radius:6px;padding:8px;' +
         'font:10px/1.7 ui-monospace,Consolas,monospace;color:#5b6470;' +
         'white-space:pre-wrap;word-break:break-all',
    empty: 'text-align:center;padding:22px 10px;color:#a0a6ae;font-size:11px',
    toast: 'position:fixed;right:18px;top:16px;max-width:300px;padding:9px 14px;' +
           'border-radius:7px;background:#2f3540;color:#fff;font-size:11px;' +
           'box-shadow:0 5px 18px rgba(0,0,0,.22);z-index:2147483600;' +
           'opacity:0;pointer-events:none;transition:opacity .25s',
    toolbar: 'display:inline-flex;align-items:center;height:32px;padding:0 14px;margin-right:8px;' +
             'background:#fff;border:1px solid #dfe3e8;border-radius:4px;color:#353535;' +
             'font-size:14px;cursor:pointer;vertical-align:middle'
  };

  // ── DOM 骨架 ──────────────────────────────────────────────────────────
  var host = el('div', 'position:fixed;right:0;top:0;width:0;height:0;z-index:2147483000');
  host.id = HOST_ID;
  document.body.appendChild(host);
  var shadow = host.attachShadow ? host.attachShadow({ mode: 'open' }) : host;

  var panel = el('div', C.panel); panel.id = 'mpui-panel'; shadow.appendChild(panel);
  var toast = el('div', C.toast); shadow.appendChild(toast);
  var launcher = el('button',
    'position:fixed;right:18px;bottom:14px;padding:7px 14px;border:0;border-radius:18px;' +
    'color:#fff;font-size:11px;cursor:pointer;box-shadow:0 2px 12px rgba(0,0,0,.18);' +
    'z-index:2147483000', '展开流水线');
  shadow.appendChild(launcher);

  var head = el('div', C.head);
  head.title = '拖动可移动面板 · 双击复位';
  head.appendChild(el('span', C.badge, TH.icon));
  var titleBox = el('div', C.title);
  head.appendChild(titleBox);
  var widthBtn = el('button', C.icon, '⇔'); widthBtn.title = '切换宽 / 窄面板';
  var foldBtn = el('button', C.icon, '−'); foldBtn.title = '折叠 / 展开内容';
  var closeBtn = el('button', C.icon, '×'); closeBtn.title = '收起面板';
  head.appendChild(widthBtn); head.appendChild(foldBtn); head.appendChild(closeBtn);
  panel.appendChild(head);

  var statusBar = el('div', C.status, '面板已加载 · 等待本机桥下发数据');
  panel.appendChild(statusBar);

  var tabsBox = el('div', C.tabs);
  [['local', '本机'], ['page', '当前页'], ['log', '输出']].forEach(function (t) {
    var b = el('button', C.tab, t[1]);
    b.setAttribute('data-tab', t[0]); b.setAttribute('data-name', t[1]);
    tabsBox.appendChild(b);
  });
  panel.appendChild(tabsBox);

  var viewLocal = el('div', C.view);
  var viewPage = el('div', C.view);
  var viewLog = el('div', C.view);
  viewLocal.id = 'mpui-view-local';
  viewPage.id = 'mpui-view-page';
  viewLog.id = 'mpui-view-log';
  viewPage.style.display = 'none'; viewLog.style.display = 'none';
  panel.appendChild(viewLocal); panel.appendChild(viewPage); panel.appendChild(viewLog);

  var searchBox = el('input', C.search);
  searchBox.placeholder = '搜索素材标题 / id …';
  searchBox.setAttribute('aria-label', '搜索素材');
  viewLocal.appendChild(searchBox);
  var chipsBox = el('div', C.chips); viewLocal.appendChild(chipsBox);
  var listBox = el('div', ''); viewLocal.appendChild(listBox);
  viewLocal.appendChild(el('div', C.h, '本机草稿台账'));
  var localDraftBox = el('div', ''); viewLocal.appendChild(localDraftBox);

  var pageBox = el('div', ''); viewPage.appendChild(pageBox);
  var logBox = el('div', C.log, '（等待操作）'); viewLog.appendChild(logBox);
  viewLog.appendChild(el('div', C.h, '运行信息'));
  var infoBox = el('div', ''); viewLog.appendChild(infoBox);
  viewLog.appendChild(el('div', C.h, '待上传图片（本地路径）'));
  var pendingBox = el('div', ''); viewLog.appendChild(pendingBox);

  var dock = el('div', C.dock);
  var picked = el('div', C.picked, '未选中素材');
  dock.appendChild(picked);
  var btnWrap = el('div', ''); dock.appendChild(btnWrap);
  panel.appendChild(dock);

  var DATA = { items: [], selected: '', logs: [], queue: [], seq: 0,
               folded: false, hidden: false, tab: 'local', filter: 'all', q: '',
               confirm: {}, confirmTimer: null, pending_upload: [], cached_drafts: [],
               total: 0, published_today: 0, probe: 'offline', sig: '', pageSig: '',
               wide: false };

  var NEED_CONFIRM = { draft: 1, published: 1 };
  // cmd, 素材库标签, 草稿箱标签, 是否主按钮, 允许出现的页面(* = 全部)
  var DEFS = [
    ['draft',      '存草稿',     '写入草稿',   1, '*'],
    ['build',      '生成预览',   '生成预览',   0, '*'],
    ['copy',       '复制排版',   '复制排版',   0, '*'],
    ['preview',    '看预览',     '看预览',     0, '*'],
    ['guide',      '发布指引',   '发布指引',   0, '*'],
    ['published',  '标记已发布', '标记已发布', 0, '*'],
    ['refresh',    '刷新',       '刷新',       0, '*'],
    ['openlocal',  '本地页',     '本地页',     0, '*'],
    ['reveal',     '目录',       '目录',       0, '*'],
    ['copypaths',  '复制路径',   '复制路径',   0, 'material']
  ];
  DEFS.forEach(function (d) {
    var b = el('button', C.btn, d[1]);
    b.setAttribute('data-cmd', d[0]);
    b.setAttribute('data-m', d[1]);
    b.setAttribute('data-d', d[2]);
    b.setAttribute('data-primary', d[3] ? '1' : '0');
    b.setAttribute('data-routes', d[4]);
    btnWrap.appendChild(b);
  });

  // ── 主题 / 路由 ───────────────────────────────────────────────────────
  function labelOf(b) {
    return b.getAttribute(R === 'draft' ? 'data-d' : 'data-m');
  }
  function primaryStyle() {
    return C.btn + ';border-color:' + TH.color + ';background:' + TH.color + ';color:#fff;font-weight:600';
  }
  function applyTheme() {
    head.style.background = 'linear-gradient(100deg,' + TH.color + ',' + TH.dark + ')';
    launcher.style.background = TH.color;
    titleBox.innerHTML = esc('本地素材库 · ' + TH.name) + '<span style="' + C.sub + '">' + esc(TH.sub) + '</span>';
    Array.prototype.forEach.call(btnWrap.querySelectorAll('button[data-cmd]'), function (b) {
      var cmd = b.getAttribute('data-cmd');
      if (!DATA.confirm[cmd]) {          // 二次确认中的按钮不要覆盖提示文字
        b.textContent = labelOf(b);      // 页面专属标签：存草稿 / 写入草稿
        b.style.cssText = b.getAttribute('data-primary') === '1' ? primaryStyle() : C.btn;
      }
      var routes = b.getAttribute('data-routes');
      b.style.display = (routes === '*' || routes === R || R === 'other') ? 'inline-block' : 'none';
    });
    Array.prototype.forEach.call(tabsBox.querySelectorAll('button[data-tab]'), function (b) {
      var on = b.getAttribute('data-tab') === DATA.tab;
      b.style.borderBottomColor = on ? TH.color : 'transparent';
      b.style.color = on ? TH.color : '#8b929c';
      b.style.fontWeight = on ? '600' : '400';
    });
  }

  function applyVisibility() {
    var inEditor = R === 'editor';
    panel.style.display = (DATA.hidden || inEditor) ? 'none' : 'flex';
    launcher.style.display = (inEditor || !DATA.hidden) ? 'none' : 'block';
    launcher.textContent = '展开 ' + TH.name + '流水线';
  }

  // ── 反馈：状态条 + Toast ──────────────────────────────────────────────
  var toastTimer = null;
  function setStatus(t, kind) {
    statusBar.textContent = String(t).slice(0, 200);
    statusBar.style.background = kind === 'warn' ? '#fff5e2' : kind === 'err' ? '#fdecec' : '#f7f8fa';
    statusBar.style.color = kind === 'warn' ? '#a9760b' : kind === 'err' ? '#b4342c' : '#5b6470';
  }
  function showToast(t, kind) {
    toast.textContent = String(t).slice(0, 100);
    toast.style.background = kind === 'err' ? '#b4342c' : kind === 'warn' ? '#a9760b' : TH.dark;
    toast.style.opacity = '1';
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.style.opacity = '0'; }, 2600);
  }
  function addLog(t) {
    var line = String(t).slice(0, 400);
    DATA.logs.unshift(line);
    if (DATA.logs.length > 12) DATA.logs.length = 12;
    logBox.textContent = DATA.logs.join('\n');
    var kind = /❌|失败|错误/.test(line) ? 'err' : /⚠/.test(line) ? 'warn' : 'ok';
    setStatus(line.replace(/^\[\d+\]\s*/, ''), kind === 'ok' ? '' : kind);
    showToast(line.replace(/^\[\d+\]\s*/, ''), kind === 'ok' ? '' : kind);
  }

  // ── 当前页信息卡（按页面定制） ────────────────────────────────────────
  function officialNames() {
    var t = document.body ? (document.body.innerText || '') : '';
    return t.split('\n').map(function (x) { return x.trim(); })
      .filter(function (x) { return /\.(png|jpe?g|gif|webp|bmp|mp4|mp3)$/i.test(x); })
      .slice(0, 6);
  }
  function scanPage() {
    var t = document.body ? (document.body.innerText || '') : '';
    if (R === 'material') {
      var m = t.match(/(图片|音频|视频|图文)（共\s*(\d+)\s*条）/);
      return { kind: 'material', label: m ? m[1] : '素材', count: m ? Number(m[2]) : null,
               names: officialNames(),
               files: document.querySelectorAll('input[type=file]').length };
    }
    if (R === 'draft' || R === 'published') {
      var q = document.querySelector('#query_tips');
      var inp = document.querySelector('input[placeholder*="标题"],input[placeholder*="关键词"]');
      var root = document.querySelector('.appmsg__list2__block') || document.body;
      var cards = Array.from(root.querySelectorAll('a[href*="appmsgid"]'))
        .map(function (a) { return (a.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 46); })
        .filter(Boolean).slice(0, 8);
      return { kind: R, label: q ? (q.innerText || '').trim() : TH.name,
               empty: /暂无素材|暂无草稿|暂无数据/.test(t),
               searchPh: inp ? inp.placeholder : '', cards: cards,
               create: Array.from(document.querySelectorAll('button'))
                         .some(function (b) { return /新的创作/.test(b.innerText || ''); }) };
    }
    return { kind: 'other', label: TH.name, url: location.pathname };
  }
  function pageSignature() {
    var p = scanPage();
    return JSON.stringify([R, p.label, p.count, p.names, p.cards, p.empty, DATA.cached_drafts]);
  }

  function drawPage() {
    var p = scanPage();
    pageBox.innerHTML = '';
    if (p.kind === 'material') {
      var big = el('div', C.big);
      big.appendChild(el('span', C.bigNum + ';color:' + TH.color, p.count == null ? '—' : p.count));
      big.appendChild(el('span', C.bigLabel, esc(p.label) + ' · 公众号官方素材'));
      big.appendChild(el('div', C.sub2, '只读读取自当前页面，未改动任何官方素材'));
      pageBox.appendChild(big);
      pageBox.appendChild(el('div', C.h, '最近素材文件'));
      if (!p.names.length) pageBox.appendChild(el('div', C.sub2, '当前页未解析到文件名'));
      p.names.forEach(function (n) { pageBox.appendChild(el('div', C.row, esc(n))); });
      pageBox.appendChild(el('div', C.h, '本地待上传'));
      var n = (DATA.pending_upload || []).length;
      pageBox.appendChild(el('div', n ? C.pillWarn : C.pillOk,
        n ? '⚠ ' + n + ' 张图片待手动上传到公众号' : '✅ 无待上传图片'));
      pageBox.appendChild(el('div', C.sub2,
        '官方页有 ' + p.files + ' 个隐藏 file input；自动塞文件属于写操作，本面板不代劳'));
    } else if (p.kind === 'draft' || p.kind === 'published') {
      var big2 = el('div', C.big);
      big2.appendChild(el('span', C.bigNum + ';color:' + TH.color,
        p.cards.length ? p.cards.length : (p.empty ? '0' : '—')));
      big2.appendChild(el('span', C.bigLabel, esc(p.label) + ' · 当前页可见条目'));
      big2.appendChild(el('div', C.sub2, p.empty
        ? '官方列表显示「暂无素材」——多因列表类型过滤，不代表后台没有草稿'
        : '只读读取自当前页面'));
      pageBox.appendChild(big2);
      if (p.cards.length) {
        pageBox.appendChild(el('div', C.h, '页面内草稿'));
        p.cards.forEach(function (x) { pageBox.appendChild(el('div', C.row, esc(x))); });
      }
      pageBox.appendChild(el('div', C.h, '本机缓存的后台草稿'));
      if (!(DATA.cached_drafts || []).length) {
        pageBox.appendChild(el('div', C.sub2, '暂无缓存（解析自后台接口响应）'));
      } else {
        DATA.cached_drafts.slice(0, 5).forEach(function (d) {
          var box = el('div', C.row);
          box.appendChild(el('div', '', '<b>' + esc(d.title || '(无标题)') + '</b>'));
          box.appendChild(el('div', C.sub2, esc(d.update_time || d.create_time || '')));
          pageBox.appendChild(box);
        });
      }
      pageBox.appendChild(el('div', C.h, '页面能力'));
      pageBox.appendChild(el('div', C.sub2,
        '搜索框：' + esc(p.searchPh || '未发现') + ' ｜ 新的创作按钮：' + (p.create ? '有' : '无')));
    } else {
      pageBox.appendChild(el('div', C.big,
        '<div style="' + C.bigNum + ';color:' + TH.color + '">' + esc(TH.icon) + '</div>' +
        '<div style="' + C.sub2 + '">当前页面：' + esc(p.url || '') +
        '<br>此页面未做专属适配，仍可使用本机素材流水线。</div>'));
    }
  }

  // ── 本机素材列表 ──────────────────────────────────────────────────────
  var STATUS_LABEL = { 'new': '待生成', 'composed': '已生成',
                       'drafted': '草稿', 'published': '已发布' };
  function statusPill(s) {
    if (s === 'published') return C.pillOk;
    if (s === 'drafted') return C.pillWarn;
    return C.pill;
  }
  function filtered() {
    var q = DATA.q.trim().toLowerCase();
    return (DATA.items || []).filter(function (it) {
      if (DATA.filter !== 'all' && it.status !== DATA.filter) return false;
      if (q && (String(it.title || '') + ' ' + String(it.id || '')).toLowerCase().indexOf(q) < 0) return false;
      return true;
    });
  }
  function drawChips() {
    chipsBox.innerHTML = '';
    [['all', '全部'], ['new', '待生成'], ['drafted', '草稿'], ['published', '已发布']].forEach(function (f) {
      var on = DATA.filter === f[0];
      var c = el('button', C.chip, f[1]);
      c.setAttribute('data-filter', f[0]);
      if (on) c.style.cssText = C.chip + ';border-color:' + TH.color + ';color:' + TH.color + ';font-weight:600';
      chipsBox.appendChild(c);
    });
  }
  function drawList() {
    listBox.innerHTML = '';
    var arr = filtered();
    if (!arr.length) {
      listBox.appendChild(el('div', C.empty,
        (DATA.items || []).length ? '没有匹配的素材，换个关键词试试' :
        '本机素材库还是空的<br><span style="font-size:10px">到「本地页」新建一篇，或点下方「目录」放入文件夹</span>'));
      var b = el('button', C.btn, '打开本地管理页');
      b.setAttribute('data-cmd', 'openlocal');
      b.style.marginTop = '8px';
      listBox.appendChild(b);
    }
    arr.forEach(function (it) {
      var on = it.id === DATA.selected;
      var card = el('div', C.card + (on ? ';border-color:' + TH.color + ';background:#f6fbf8' : ''));
      card.setAttribute('data-mid', it.id);
      var row = el('div', 'display:flex;align-items:center;gap:6px');
      row.appendChild(el('div', C.name, esc(it.title || it.id)));
      row.appendChild(el('span', statusPill(it.status), STATUS_LABEL[it.status] || it.status));
      card.appendChild(row);
      var nImg = Array.isArray(it.images) ? it.images.length : (it.images || 0);
      card.appendChild(el('div', C.sub2,
        it.body_len + ' 字 · ' + nImg + ' 图 · ' + esc(it.id)));
      listBox.appendChild(card);
    });
    picked.textContent = DATA.selected ? '已选中：' + DATA.selected + ' · ' + TH.name + '视图'
                                       : '点上面卡片选中一篇素材';
  }
  function drawLocalDrafts() {
    localDraftBox.innerHTML = '';
    var ds = (DATA.items || []).filter(function (x) { return x.status === 'drafted'; });
    if (!ds.length) {
      localDraftBox.appendChild(el('div', C.sub2, '暂无已存草稿的素材'));
      return;
    }
    ds.forEach(function (d) {
      localDraftBox.appendChild(el('div', C.row, '✅ ' + esc(d.title || d.id) + ' · ' + esc(d.id)));
    });
  }
  function drawLogExtra() {
    pendingBox.innerHTML = '';
    var p = DATA.pending_upload || [];
    if (!p.length) { pendingBox.appendChild(el('div', C.sub2, '暂无待上传图片')); }
    else p.slice(0, 8).forEach(function (x) {
      var row = el('div', C.row, '· ' + esc(String(x).split(/[\\/]/).pop()));
      row.title = x; pendingBox.appendChild(row);
    });
    infoBox.innerHTML = '';
    [['面板版本', 'v' + V + ' · ' + TH.name + '视图'],
     ['探针状态', DATA.probe === 'LOGGED_IN' ? '在线' : '离线'],
     ['本机素材', DATA.total + ' 篇'],
     ['今日发布', DATA.published_today + ' 篇'],
     ['快捷键', 'Alt+M 折叠 · Alt+1/2/3 切页签 · Esc 收起']].forEach(function (kv) {
      infoBox.appendChild(el('div', C.row, esc(kv[0]) + '：' + esc(kv[1])));
    });
  }

  function redraw() {
    var keep = [viewLocal.scrollTop, viewPage.scrollTop, viewLog.scrollTop];
    drawChips(); drawList(); drawLocalDrafts(); drawPage(); drawLogExtra();
    viewLocal.scrollTop = keep[0]; viewPage.scrollTop = keep[1]; viewLog.scrollTop = keep[2];
  }

  // ── 动作 ──────────────────────────────────────────────────────────────
  function copyPaths() {
    var p = DATA.pending_upload || [];
    if (!p.length) return addLog('⚠️ 暂无待上传图片路径');
    var txt = p.join('\n');
    var done = function () { addLog('✅ 已复制 ' + p.length + ' 条待上传路径'); };
    var fallback = function () {
      try {
        var ta = el('textarea', 'position:fixed;left:-9999px;top:0');
        ta.value = txt; shadow.appendChild(ta); ta.select();
        var ok = document.execCommand('copy');
        shadow.removeChild(ta);
        ok ? done() : addLog('❌ 复制失败，请到「输出」页手动选取');
      } catch (e) { addLog('❌ 复制失败：' + e); }
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try { navigator.clipboard.writeText(txt).then(done, fallback); } catch (e) { fallback(); }
    } else fallback();
  }
  function clearConfirm() {
    for (var k in DATA.confirm) delete DATA.confirm[k];
    if (DATA.confirmTimer) { clearTimeout(DATA.confirmTimer); DATA.confirmTimer = null; }
    Array.prototype.forEach.call(btnWrap.querySelectorAll('button[data-cmd]'), function (b) {
      b.textContent = labelOf(b);
      b.style.cssText = b.getAttribute('data-primary') === '1' ? primaryStyle() : C.btn;
      var routes = b.getAttribute('data-routes');
      b.style.display = (routes === '*' || routes === R || R === 'other') ? 'inline-block' : 'none';
    });
  }
  function call(cmd) {
    if (cmd === 'copypaths') return copyPaths();
    if (NEED_CONFIRM[cmd]) {
      var now = Date.now();
      if (!(DATA.confirm[cmd] && now - DATA.confirm[cmd] < 9000)) {
        clearConfirm();
        DATA.confirm[cmd] = now;
        var b = btnWrap.querySelector('[data-cmd="' + cmd + '"]');
        if (b) { b.textContent = '再点一次确认'; b.style.cssText = C.btnWarn; }
        DATA.confirmTimer = setTimeout(clearConfirm, 9000);
        return addLog('⚠️ 9 秒内再点一次「' + labelOf(b || { getAttribute: function () { return cmd; } }) + '」确认执行');
      }
      clearConfirm();
    }
    var args = {};
    if (cmd !== 'refresh' && cmd !== 'openlocal' && cmd !== 'reveal') {
      if (!DATA.selected) return addLog('❌ 请先点选一篇素材');
      args.id = DATA.selected;
    }
    var seq = ++DATA.seq;
    DATA.queue.push({ seq: seq, cmd: cmd, args: args });
    addLog('→ ' + cmd + (args.id ? ' ' + args.id : '') + ' 已提交');
    return seq;
  }
  function setTab(name) {
    DATA.tab = name;
    viewLocal.style.display = name === 'local' ? 'block' : 'none';
    viewPage.style.display = name === 'page' ? 'block' : 'none';
    viewLog.style.display = name === 'log' ? 'block' : 'none';
    applyTheme();
    savePos();
  }

  // 事件委托：容器只建一次，子元素重建不丢事件
  function closest(e, sel) {
    return e.target && e.target.closest ? e.target.closest(sel) : null;
  }
  btnWrap.addEventListener('click', function (e) {
    var b = closest(e, 'button[data-cmd]');
    if (b) call(b.getAttribute('data-cmd'));
  });
  listBox.addEventListener('click', function (e) {
    var b = closest(e, 'button[data-cmd]');
    if (b) return call(b.getAttribute('data-cmd'));
    var card = closest(e, '[data-mid]');
    if (!card) return;
    DATA.selected = card.getAttribute('data-mid');
    drawList();
    showToast('已选中 ' + DATA.selected);
  });
  chipsBox.addEventListener('click', function (e) {
    var b = closest(e, '[data-filter]');
    if (!b) return;
    DATA.filter = b.getAttribute('data-filter');
    drawChips(); drawList();
  });
  tabsBox.addEventListener('click', function (e) {
    var b = closest(e, '[data-tab]');
    if (b) setTab(b.getAttribute('data-tab'));
  });
  searchBox.addEventListener('input', function () {
    DATA.q = searchBox.value; drawList();
  });
  statusBar.onclick = function () { setTab('log'); };

  // ── 工具栏按钮 / 位置记忆 / 拖动 / 快捷键 ────────────────────────────
  function ensureButton() {
    var btn = document.getElementById(BTN_ID);
    if (btn && btn.isConnected) return true;
    var up = null;
    var all = document.querySelectorAll('button');
    for (var i = 0; i < all.length; i++) {
      var t = (all[i].innerText || '').trim();
      if ((t === '上传' || t === '新的创作') && all[i].getBoundingClientRect().width > 0) {
        up = all[i]; break;
      }
    }
    if (!up || !up.parentNode) return false;
    btn = el('button', C.toolbar, '● 本地素材库');
    btn.id = BTN_ID;
    btn.title = '打开本机素材库流水线面板';
    btn.onclick = function () { DATA.hidden = false; applyVisibility(); };
    up.parentNode.insertBefore(btn, up);
    return true;
  }

  var POS_KEY = 'mpui.v4.pos';
  function savePos() {
    try {
      localStorage.setItem(POS_KEY, JSON.stringify({
        left: panel.style.left, top: panel.style.top,
        right: panel.style.right, wide: DATA.wide, tab: DATA.tab
      }));
    } catch (e) {}
  }
  function loadPos() {
    try {
      var o = JSON.parse(localStorage.getItem(POS_KEY) || 'null');
      if (!o) return;
      if (o.left) { panel.style.left = o.left; panel.style.right = 'auto'; }
      if (o.top) panel.style.top = o.top;
      if (o.wide) { DATA.wide = true; panel.style.width = '420px'; }
      if (o.tab && /^(local|page|log)$/.test(o.tab)) DATA.savedTab = o.tab;
    } catch (e) {}
  }
  function resetPos() {
    panel.style.left = ''; panel.style.top = ''; panel.style.right = '16px';
    DATA.wide = false; panel.style.width = '';
    savePos(); applyTheme();
    showToast('面板位置与宽度已复位');
  }

  var drag = null;
  function onMove(e) {
    if (!drag) return;
    var x = Math.max(0, Math.min(window.innerWidth - panel.offsetWidth, e.clientX - drag.dx));
    var y = Math.max(0, Math.min(window.innerHeight - 60, e.clientY - drag.dy));
    panel.style.left = x + 'px'; panel.style.top = y + 'px'; panel.style.right = 'auto';
  }
  function onUp() {
    if (!drag) return;
    drag = null;
    document.removeEventListener('mousemove', onMove, true);
    document.removeEventListener('mouseup', onUp, true);
    savePos();
  }
  head.addEventListener('mousedown', function (e) {
    if (e.target.tagName === 'BUTTON') return;
    var r = panel.getBoundingClientRect();
    drag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
    document.addEventListener('mousemove', onMove, true);
    document.addEventListener('mouseup', onUp, true);
    e.preventDefault();
  });
  head.addEventListener('dblclick', resetPos);
  widthBtn.onclick = function () {
    DATA.wide = !DATA.wide;
    panel.style.width = DATA.wide ? '420px' : '';
    savePos(); showToast(DATA.wide ? '已切换为宽面板' : '已切换为窄面板');
  };
  foldBtn.onclick = function () {
    DATA.folded = !DATA.folded;
    dock.style.display = DATA.folded ? 'none' : 'block';
    tabsBox.style.display = DATA.folded ? 'none' : 'flex';
    [viewLocal, viewPage, viewLog].forEach(function (v) {
      v.style.display = (!DATA.folded && v.id === 'mpui-view-' + DATA.tab) ? 'block' : 'none';
    });
    foldBtn.textContent = DATA.folded ? '+' : '−';
  };
  closeBtn.onclick = function () { DATA.hidden = true; applyVisibility(); };
  launcher.onclick = function () { DATA.hidden = !DATA.hidden; applyVisibility(); };

  document.addEventListener('keydown', function (e) {
    if (!e.altKey && e.key !== 'Escape') return;
    var tag = e.target && e.target.tagName;
    if (/INPUT|TEXTAREA/.test(tag) || (e.target && e.target.isContentEditable)) return;
    if (e.altKey && e.key.toLowerCase() === 'm') { foldBtn.click(); e.preventDefault(); }
    else if (e.altKey && /^[123]$/.test(e.key)) {
      setTab(['local', 'page', 'log'][Number(e.key) - 1]); e.preventDefault();
    } else if (e.key === 'Escape') { DATA.hidden = true; applyVisibility(); }
  }, true);

  function tickUI() {
    ensureButton();
    var nr = routeOf();
    if (nr !== R) {                       // SPA 内切页：只换主题与视图，不重建按钮
      R = nr; TH = THEMES[R] || THEMES.other;
      applyTheme(); applyVisibility(); redraw();
      showToast('已切换到「' + TH.name + '」视图');
      return;
    }
    applyVisibility();
    var ps = pageSignature();
    if (ps !== DATA.pageSig) { DATA.pageSig = ps; drawPage(); }
  }

  window.__mpui = {
    v: V, ready: true, route: function () { return R; },
    take: function () { return JSON.stringify(DATA.queue.splice(0)); },
    render: function (d) {
      if (!d) return;
      DATA.total = d.total || 0;
      DATA.published_today = d.published_today || 0;
      DATA.probe = d.probe || 'offline';
      DATA.items = d.items || [];
      DATA.pending_upload = d.pending_upload || [];
      DATA.cached_drafts = d.cached_drafts || [];
      if (!DATA.selected && DATA.items.length) DATA.selected = DATA.items[0].id;
      if (DATA.selected && !DATA.items.some(function (x) { return x.id === DATA.selected; })) {
        DATA.selected = DATA.items.length ? DATA.items[0].id : '';
      }
      var sig = JSON.stringify([DATA.items, DATA.selected, DATA.total,
                                DATA.published_today, DATA.probe,
                                DATA.pending_upload, DATA.cached_drafts, R]);
      if (sig !== DATA.sig) { DATA.sig = sig; redraw(); }
      DATA.pageSig = pageSignature();
    },
    onResult: function (seq, res) {
      var t = '[' + seq + '] ';
      if (res && res.ok === false) t += '❌ ' + (res.error || res.output || '失败');
      else t += '✅ ' + (res && (res.message || res.output)
        ? String(res.message || res.output).slice(0, 260) : '完成');
      addLog(t);
    },
    ensureButton: ensureButton,
    show: function () { DATA.hidden = false; applyVisibility(); },
    applyVisibility: applyVisibility,
    setTab: setTab,
    log: addLog
  };

  loadPos();
  applyTheme();
  setTab(DATA.savedTab || 'local');
  redraw();
  applyVisibility();
  DATA.pageSig = pageSignature();
  window.__mpuiTimer = setInterval(tickUI, 1800);
  addLog('面板 v4 已加载 · 「' + TH.name + '」专属视图 · 拖标题栏可移动');
})();
