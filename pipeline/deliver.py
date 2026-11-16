# -*- coding: utf-8 -*-
"""CDP 投递层：把生成好的内容送进公众号编辑器，保存草稿。

复用 MpLoginProbe v3 的 127.0.0.1:8765 通道（cdp_vision.Cdp）。

2026-11 版编辑器的硬约束（实测，别再踩）：
  * 正文是 ProseMirror `#ueditor_0 .ProseMirror`（mock-iframe，仍在主文档）。
  * 带样式正文的三条可用通道（2026-11-15 复测更新）：
    1) 合成 ClipboardEvent('paste') + DataTransfer 塞 text/html —— ✅ 推荐，
       走 PM 的 paste handler 进 state，保存后重载不丢。见 tools/paste_html.py。
       注意：必须用 DataTransfer 构造 clipboardData；裸 dispatchEvent 不带
       clipboardData 时 handler 读不到内容，等于没粘。
    2) CDP Input.insertText —— 只能写纯文本（逐段 + Enter 分段）。
    3) execCommand('insertHTML') / 裸 DOM 改写 —— 只改 DOM 不进 state，
       保存时按 state 序列化会被打回原样，禁止用于正文。
    系统剪贴板路径（CF_HTML + Ctrl+V，无论 CDP 还是 SendInput）受窗口
    前台/最小化状态摆布：WebView2 失焦或最小化时 Chromium 拒绝读剪贴板，
    静默失败，不要依赖。
  * 标题同理，用 CDP 点击 + Ctrl+A + insertText；合成 paste 会追加成两遍。
  * 作者 / 摘要是普通表单，用原生 setter + input/change 事件即可。

安全：草稿模式（默认）只写自己的草稿，任何路径都不点群发按钮。
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(BASE))
sys.path.insert(0, BASE)
from cdp_vision import Cdp, CdpError, OUT as EXPLORE_DIR  # noqa: E402

from contract import State  # noqa: E402

CONFIRM_WORD = "I_UNDERSTAND"
BANNER = ("⚠️ 即将执行【群发发布】，这会真实推送给关注者且不可撤回。"
          "请确认标题、正文、封面、图片均已核对。")


# ── 基础 CDP 工具 ────────────────────────────────────────────────────────
def ev(cdp, expr, await_promise=False):
    """执行 JS 并取回值。/cdp 返回 {ok,method,result:{result:{value:...}}}"""
    r = cdp.cdp("Runtime.evaluate", {
        "expression": expr,
        "returnByValue": True,
        "awaitPromise": await_promise,
        "userGesture": True,
    })
    if not r.get("ok"):
        raise CdpError("Runtime.evaluate 失败: %s" % str(r)[:300])
    res = r.get("result", {})
    if "exceptionDetails" in res:
        raise CdpError("JS 异常: %s" % str(res["exceptionDetails"])[:300])
    return res.get("result", {}).get("value")


def token_of(cdp):
    s = cdp.status()
    m = re.search(r"[?&]token=(\d+)", s.get("url", ""))
    if m:
        return m.group(1)
    p = os.path.join(os.path.dirname(BASE), "nav_history.txt")
    if os.path.exists(p):
        for line in reversed(open(p, encoding="utf-8").read().splitlines()):
            m = re.search(r"[?&]token=(\d+)", line)
            if m:
                return m.group(1)
    raise CdpError("拿不到 token，请先在后台任意页面停留一次")


# ── 只读探测 ────────────────────────────────────────────────────────────
PROBE_JS = r"""(function () {
  const q = s => Array.from(document.querySelectorAll(s));
  const pick = e => ({
    tag: e.tagName, id: e.id || '', cls: (e.className || '').toString().slice(0, 120),
    name: e.name || '', placeholder: e.placeholder || '',
    text: (e.innerText || e.value || '').trim().slice(0, 60),
    contenteditable: e.getAttribute('contenteditable') || '',
    type: e.type || '', href: (e.href || '').slice(0, 160), src: (e.src || '').slice(0, 160)
  });
  return {
    url: location.href, title: document.title,
    editable: q('[contenteditable="true"], [contenteditable=""], textarea').slice(0, 25).map(pick),
    iframes: q('iframe').slice(0, 20).map(pick),
    inputs: q('input').slice(0, 40).map(pick),
    buttons: q('button, [role="button"], a.btn, .weui-desktop-btn').slice(0, 60).map(pick)
               .filter(x => x.text),
    fileInputs: q('input[type="file"]').slice(0, 20).map(pick),
    bodyLen: document.body ? document.body.innerHTML.length : 0
  };
})()"""


def probe(cdp, save=True, name="editor_probe"):
    """只读探测当前页面结构；不点击、不输入、不跳转。"""
    st = cdp.status()
    if "LOGGED_IN" not in str(st.get("state", "")).upper():
        raise CdpError("未登录：%s" % st.get("state"))
    data = {"probe_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": st, "main": ev(cdp, PROBE_JS)}
    try:
        tree = cdp.cdp("Page.getFrameTree", {})
        data["frames"] = tree.get("result", {}).get("frameTree", {})
    except Exception as e:
        data["frames_error"] = str(e)
    try:
        doc = cdp.cdp("DOM.getDocument", {"depth": -1, "pierce": True})
        root = doc.get("result", {}).get("root", {}).get("nodeId")
        if root:
            outer = cdp.cdp("DOM.getOuterHTML", {"nodeId": root})
            raw = outer.get("result", {}).get("outerHTML", "")
            data["dom_len"] = len(raw)
            path = os.path.join(EXPLORE_DIR, name + "_dom.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(raw[:2000000])
            data["dom_file"] = path
    except Exception as e:
        data["dom_error"] = str(e)
    if save:
        os.makedirs(EXPLORE_DIR, exist_ok=True)
        p = os.path.join(EXPLORE_DIR, name + ".json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        data["saved"] = p
    return data


def navigate_editor(cdp):
    """打开新建图文编辑器（只做导航，不落任何内容）。"""
    t = token_of(cdp)
    url = ("https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit"
           "&action=edit&type=77&isNew=1&token=%s&lang=zh_CN" % t)
    r = cdp.cdp("Page.navigate", {"url": url})
    if not r.get("ok"):
        raise CdpError("导航失败: %s" % str(r)[:200])
    for _ in range(30):
        time.sleep(0.5)
        u = str(cdp.status().get("url", ""))
        if "appmsg_edit" in u:
            try:
                if ev(cdp, "document.readyState") == "complete":
                    return u
            except CdpError:
                pass                      # 上下文正在销毁，下一轮再问
    raise CdpError("编辑器加载超时，最后 URL=%s" % str(cdp.status().get("url", "")))


SELECTOR_JS = r"""(function () {
  const info = s => { const e = document.querySelector(s); if (!e) return {found:false};
    const r = e.getBoundingClientRect();
    return {found:true, tag:e.tagName, editable:!!e.isContentEditable,
            visible:r.width>0 && r.height>0, len:(e.innerHTML||e.value||'').length}; };
  return {url: location.href,
          body: info('#ueditor_0 .ProseMirror'),
          title: info('.title-editor__input .ProseMirror'),
          titleMirror: info('#title'),
          author: info('#author'),
          digest: info('#js_description'),
          fileInput: info('input[type=file]'),
          publishBtn: info('button.mass_send'),
          supportsPaste: typeof ClipboardEvent !== 'undefined' && typeof DataTransfer !== 'undefined'};
})()"""


def verify(cdp):
    """纯只读：确认真实编辑器选择器仍有效（微信改版后第一步就该跑它）。"""
    return ev(cdp, SELECTOR_JS)


def read_editor(cdp):
    """只读：把编辑器关键字段一次性读回，用于写入后校验。"""
    return ev(cdp, r"""(function () {
  var t = document.querySelector('.title-editor__input .ProseMirror');
  var m = document.querySelector('#title');
  var b = document.querySelector('#ueditor_0 .ProseMirror');
  var a = document.querySelector('#author');
  var d = document.querySelector('#js_description');
  return {url: location.href,
          title_pm: t ? t.innerText.trim() : null,
          title_mirror: m ? m.value : null,
          body_html_len: b ? b.innerHTML.length : null,
          body_text: b ? b.innerText.slice(0, 120) : null,
          author: a ? a.value : null,
          digest: d ? d.value : null};
})()""")


# ── 键盘路径（唯一可靠写入通道）──────────────────────────────────────────
FOCUS_BODY_JS = r"""(function () {
  var e = document.querySelector('#ueditor_0 .ProseMirror');
  if (!e) return null;
  e.focus();
  var s = window.getSelection(), r = document.createRange();
  r.selectNodeContents(e);
  s.removeAllRanges(); s.addRange(r);
  return {sameNode: document.activeElement === e};
})()"""

SET_FIELDS_JS = r"""(function (cfg) {
  var out = {};
  var set = function (sel, v, key) {
    var el = document.querySelector(sel);
    if (!el || !v) { out[key] = false; return; }
    var d = Object.getOwnPropertyDescriptor(el.__proto__, 'value');
    if (d && d.set) d.set.call(el, v); else el.value = v;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    out[key] = el.value === v;
  };
  set('#author', cfg.author, 'author');
  set('#js_description', cfg.digest, 'digest');
  return out;
})"""


def focus_select_all(cdp, selector):
    """CDP 真实点击 + Ctrl+A，建立浏览器原生选区（PM 会正确感知）。"""
    pos = ev(cdp, """
(function () {
  var e = document.querySelector(%s);
  if (!e) return null;
  e.scrollIntoView({block: 'center'});
  var r = e.getBoundingClientRect();
  return {x: r.x + Math.min(r.width / 2, 150), y: r.y + r.height / 2};
})()""" % json.dumps(selector))
    if not pos:
        raise CdpError("找不到元素 %s" % selector)
    click_css(cdp, pos["x"], pos["y"])
    time.sleep(0.2)
    cdp.key("Ctrl+A")
    time.sleep(0.15)
    return pos


def replace_title_via_keys(cdp, title):
    """标题走 CDP 键盘；合成 paste 对 PM 标题是追加，只有真实键盘才替换。"""
    focus_select_all(cdp, '.title-editor__input .ProseMirror')
    cdp.text(title)
    time.sleep(0.35)
    return read_editor(cdp)


def fragment_to_paragraphs(fragment):
    """把排版 fragment 拆成纯文本段落数组（2026-11-15 新增）。

    旧逻辑把整个 fragment 的空白压成单空格，正文全挤成一大段，
    「分段 / 排版」全丢。现在按块级标签断行：
      * <p>/<h1-6>/<blockquote>/<li>/<section>/<div> 结束处断行，<br> 断行；
      * <table> 的每个 <tr> 转成一行，单元格用「 ｜ 」连接（|---| 分隔行丢弃）。
    纯文本视觉增强（2026-11-15 二次优化，无法带样式的补救）：
      * 小节标题加「▍」前缀（公众号常见的标题装饰符）；
      * 引用块加「❝ 」前缀；
      * 分隔线 <p border-top> 转成字符线「· · · · · ·」。
    """
    t = fragment
    # 纯文本装饰：标题 / 引用 / 分隔线 / 图注标记
    t = re.sub(r"<h[12][^>]*>", lambda m: m.group(0) + "▍", t)
    t = re.sub(r"<blockquote[^>]*>", lambda m: m.group(0) + "❝ ", t)
    t = re.sub(r'(<p style="[^"]*text-align:\s*center[^"]*">)',
               lambda m: m.group(1) + "\x00", t)
    t = re.sub(r'<p style="[^"]*border-top[^"]*"[^>]*>\s*</p>',
               "\n· · · · · ·\n", t)
    # 表格行 → 一行文本
    def _tr(m):
        tds = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", m.group(1), re.S)
        cells = [re.sub(r"<[^>]+>", "", x).strip() for x in tds]
        cells = [c for c in cells if c]
        if not cells or all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
            return ""
        return "\n" + " ｜ ".join(cells)
    t = re.sub(r"<tr[^>]*>(.*?)</tr>", _tr, t, flags=re.S)
    t = re.sub(r"<br\s*/?>", "\n", t)
    t = re.sub(r"</(p|h[1-6]|blockquote|li|section|div)>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    paras = [ln.strip() for ln in t.split("\n") if ln.strip()]
    # 首行缩进两格（2026-11-15）：只缩普通正文段；
    # 标题 ▍ / 引用 ❝ / 列表 • / 有序列表 / 表格行 ｜ / 分隔线 · 不缩；
    # 图注段（compose CAP 是 text-align:center，已在块头打了 \x00 标记）不缩
    out = []
    for p in paras:
        if p.startswith("\x00"):
            out.append(p.lstrip("\x00"))
        elif (p.startswith(("▍", "❝", "•", "· · ·"))
                or re.match(r"^\d+[.、．]\s", p)
                or " ｜ " in p):
            out.append(p)
        else:
            out.append("　　" + p)
    return out


def fill(cdp, fragment, title, author="", digest="", dry_run=False,
         min_body_len=200):
    """填标题 / 正文 / 作者 / 摘要。不点保存，不群发。

    正文逐段写入（2026-11-15 改造）：先按块级标签拆段，再
    cdp.text(段落) + Enter 键分段——Enter 是真实键盘事件，PM 会正确
    拆成 <section> 段落（已实测）；合成 \n 会被 HTML 空白折叠吃掉。
    标题段（h2）前后额外空一行，视觉上更分明。
    带内联样式的 HTML 由 `copy` 命令放进剪贴板，供真人在编辑器里 Ctrl+V
    覆盖，那一步才会保留完整排版。
    """
    paras = fragment_to_paragraphs(fragment)
    plain_len = sum(len(p) for p in paras)
    plan = {
        "editor_url": str(cdp.status().get("url", "")),
        "title": title, "author": author, "digest": digest[:40],
        "html_len": len(fragment), "plain_len": plain_len,
        "paras": len(paras),
        "steps": ["预检选择器", "JS 聚焦正文 + 全选", "逐段 CDP insertText + Enter 分段",
                  "CDP 键盘写标题", "填作者 / 摘要", "读回校验"],
    }
    if dry_run:
        plan["selectors"] = verify(cdp)
        return {"ok": True, "dry_run": True, "plan": plan}

    chk = verify(cdp)
    if not (chk.get("body", {}).get("found") and chk.get("title", {}).get("found")):
        raise CdpError("编辑器选择器失效，先跑 probe：%s" % chk)

    f = ev(cdp, FOCUS_BODY_JS)
    if not f or not f.get("sameNode"):
        raise CdpError("正文编辑器没能获得焦点：%s" % f)
    cdp.key("Backspace")                  # FOCUS_BODY_JS 已全选，清空旧正文
    time.sleep(0.3)
    for i, p in enumerate(paras):         # 逐段写入：text + Enter 真实分段
        # 2026-11-15 三次调整：统一单 Enter（双 Enter 会产生空段落，
        # 用户反馈段落间距太大）；段间距交给微信默认 margin
        if i:
            cdp.key("Enter")
            time.sleep(0.1)
        cdp.text(p)
        time.sleep(0.1)
    time.sleep(0.4)

    kb = replace_title_via_keys(cdp, title)
    if kb.get("title_pm") != title and kb.get("title_mirror") != title:
        raise CdpError("标题写入失败：%s" % json.dumps(kb, ensure_ascii=False)[:300])

    fields = ev(cdp, SET_FIELDS_JS + "("
                + json.dumps({"author": author, "digest": digest},
                             ensure_ascii=False) + ")")
    st = read_editor(cdp)
    plan["result"] = {"body_html_len": st["body_html_len"],
                      "body_text": st["body_text"],
                      "title_pm": st["title_pm"],
                      "title_mirror": st["title_mirror"],
                      "fields": fields, "focus": f}
    if (st["body_html_len"] or 0) < min_body_len:
        raise CdpError("正文写入后仍为空（阈值 %d）：%s"
                       % (min_body_len, json.dumps(st, ensure_ascii=False)[:300]))
    return {"ok": True, "plan": plan}


# ── 剪贴板：把带排版的 HTML 交给真人 Ctrl+V ──────────────────────────────
def copy_fragment_to_clipboard(fragment):
    """把 fragment 放进 Windows CF_HTML 剪贴板（供编辑器真实粘贴）。

    CDP 自己发的 Ctrl+V 不触发浏览器 paste，但真人按 Ctrl+V 会带
    isTrusted 事件，微信编辑器能正常解析内联样式并转存图片。
    """
    import ctypes
    data = _make_cf_html(fragment)
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
    u32.RegisterClipboardFormatW.restype = ctypes.c_uint
    u32.OpenClipboard.argtypes = [ctypes.c_void_p]
    u32.EmptyClipboard.argtypes = []
    u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    u32.CloseClipboard.argtypes = []
    k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    fmt = u32.RegisterClipboardFormatW("HTML Format")
    if not u32.OpenClipboard(0):
        raise CdpError("OpenClipboard 失败（剪贴板被别的程序占用）")
    try:
        u32.EmptyClipboard()
        h = k32.GlobalAlloc(0x0002, len(data) + 1)
        p = k32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        ctypes.memset(p + len(data), 0, 1)
        k32.GlobalUnlock(h)
        if not u32.SetClipboardData(fmt, h):
            raise CdpError("SetClipboardData 失败")
    finally:
        u32.CloseClipboard()
    return {"ok": True, "format": fmt, "bytes": len(data)}


def _make_cf_html(fragment):
    frag = fragment.encode("utf-8")
    pre = b"<html><body>\r\n<!--StartFragment-->"
    post = b"<!--EndFragment-->\r\n</body></html>"
    tpl = (b"Version:0.9\r\nStartHTML:%010d\r\nEndHTML:%010d\r\n"
           b"StartFragment:%010d\r\nEndFragment:%010d\r\n")
    sh = len(tpl % (0, 0, 0, 0))
    sf = sh + len(pre)
    ef = sf + len(frag)
    eh = ef + len(post)
    return (tpl % (sh, eh, sf, ef)) + pre + frag + post


# ── 按钮 ────────────────────────────────────────────────────────────────
def click_css(cdp, x, y):
    """DOM getBoundingClientRect 给的是 CSS 像素，直接送 /click；
    不要走 cdp.click_px（那个入参是截图 PNG 像素，会再除一次 dpr 导致点偏）。"""
    return json.loads(cdp._req("/click", {"x": x, "y": y}))


def click_by_text(cdp, words, timeout=12, forbid=()):
    """点网页里文字匹配的按钮。forbid 在点击前拦截危险词，防误点群发。"""
    expr = """
(function () {
  const keys = %s;
  const t = e => (e.innerText || '').trim();
  const els = Array.from(document.querySelectorAll(
    'button, [role="button"], .weui-desktop-btn, a'))
    .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
  let hit = null;
  for (const k of keys) { hit = els.find(e => t(e) === k); if (hit) break; }
  if (!hit) for (const k of keys) { hit = els.find(e => t(e).includes(k)); if (hit) break; }
  if (!hit) return null;
  hit.scrollIntoView({block:'center', inline:'center'});
  const r = hit.getBoundingClientRect();
  return {text: t(hit), x: r.x + r.width/2, y: r.y + r.height/2};
})()""" % json.dumps(words, ensure_ascii=False)
    end = time.time() + timeout
    while time.time() < end:
        hit = ev(cdp, expr)
        if hit:
            label = str(hit.get("text", ""))
            bad = [w for w in forbid if w in label]
            if bad:
                raise CdpError("拒绝点击危险按钮「%s」（命中禁用词 %s）"
                               % (label, "/".join(bad)))
            click_css(cdp, hit["x"], hit["y"])
            return hit
        time.sleep(0.6)
    raise CdpError("找不到按钮：%s" % words)


def save_draft(cdp, dry_run=False):
    if dry_run:
        return {"ok": True, "dry_run": True,
                "plan": "点「保存为草稿」（button，需滚动到可见区）"}
    # 2026-11-15 修复：探针视口只有 ~438x473，而保存按钮在 x≈469——
    # 一半在视口外，坐标点击无效（之前"报成功实际没保存"的根因）。
    # 保存前先放大视口（同 media_inject.VIEWPORT），点完还原。
    cdp.cdp("Emulation.setDeviceMetricsOverride",
            {"width": 1400, "height": 1400, "deviceScaleFactor": 1,
             "mobile": False})
    time.sleep(0.6)
    try:
        hit = None
        for attempt in range(3):          # 最多点 3 次，直到看到保存成功反馈
            hit = click_by_text(cdp, ["保存为草稿", "保存草稿", "存草稿"],
                                forbid=("发表", "群发", "发布", "推送", "确认"))
            time.sleep(2.5)
            ok = ev(cdp, r"""(function(){
              var t=document.body.innerText||'';
              if(/保存成功|已保存/.test(t)) return true;
              // 编辑页 URL 变成 list / 草稿箱也算成功
              return /appmsgpublish|appmsg\?t=media\/appmsg_list/.test(location.href);
            })()""")
            if ok:
                break
    finally:
        cdp.cdp("Emulation.clearDeviceMetricsOverride")
        time.sleep(0.3)
    return {"ok": True, "clicked": hit, "url": str(cdp.status().get("url", ""))}


def publish(cdp, mid, cfg, manifest, state, confirm="", force=False, dry_run=False):
    """唯一会真实群发的入口。草稿模式下任何路径都点不到群发按钮。"""
    gates, blocked = [], False

    def gate(name, ok, note=""):
        nonlocal blocked
        gates.append({"gate": name, "ok": bool(ok), "note": note})
        if not ok:
            blocked = True

    mode = (cfg.get("delivery") or {}).get("mode", "draft_only")
    gate("运行模式允许自动群发（mode=auto_publish）",
         mode == "auto_publish", "当前 mode=%s" % mode)
    gate("已显式确认 --confirm " + CONFIRM_WORD, confirm == CONFIRM_WORD)
    gate("台账中未发布过该素材", state.get(mid).get("status") != "published",
         str(state.get(mid).get("status")))
    limit = int((cfg.get("delivery") or {}).get("daily_limit", 1))
    used = state.count_published_on()
    gate("当日群发额度 %d（已用 %d）" % (limit, used), used < limit)
    gate("文章无 ❌ 待填项", (not manifest.get("has_blocker")) or force,
         "有 %d 项" % len(manifest.get("checks", [])))
    gate("已保存过草稿", state.get(mid).get("status") in ("drafted", "published"))
    gate("登录态正常", "LOGGED_IN" in str(cdp.status().get("state", "")).upper())

    result = {"ok": False, "gates": gates, "blocked": blocked,
              "banner": BANNER, "title": manifest.get("title")}
    if dry_run:
        result["dry_run"] = True
        result["ok"] = not blocked
        return result
    if blocked or mode != "auto_publish":
        return result                  # 草稿模式：绝不点群发

    hit = click_by_text(cdp, ["发表", "群发", "发布"])
    time.sleep(2)
    try:
        hit2 = click_by_text(cdp, ["确定", "确认发表", "继续发表"], timeout=6)
    except CdpError:
        hit2 = None
    time.sleep(2)
    rec = state.set(mid, "published",
                    published_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    title=manifest.get("title"),
                    clicked=hit.get("text") if hit else "")
    result.update({"ok": True, "clicked": hit, "confirm_clicked": hit2,
                   "url": str(cdp.status().get("url", "")), "record": rec})
    return result
