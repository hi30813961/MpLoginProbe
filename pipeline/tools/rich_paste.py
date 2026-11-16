"""把剪贴板里的排版 HTML 粘进当前公众号编辑器（替代人工 Ctrl+A / Ctrl+V）。

前提：
  1) 已跑过 python pipeline.py copy --id <素材>，剪贴板里是 CF_HTML
  2) 探针 8765 在线，且当前页面正停在那篇草稿的编辑器

用法：
    python tools/rich_paste.py            # 全选正文 -> 粘贴 -> 校验
    python tools/rich_paste.py --dry      # 只看当前正文状态，不动手
    python tools/rich_paste.py --save     # 粘完顺手点「保存为草稿」

为什么不用 execCommand('paste')：页面脚本被安全策略禁止读剪贴板，
只有真实的 Ctrl+V 键序列才会走浏览器自己的粘贴管线。
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
BODY = '#ueditor_0 .ProseMirror'

STATE_JS = """(function(){
  var b = document.querySelector('%s');
  if (!b) return {ok:false, why:'找不到正文编辑器'};
  var html = b.innerHTML || '';
  return {
    ok: true,
    text_len: (b.innerText || '').length,
    html_len: html.length,
    style_tags: (html.match(/style="/g) || []).length,
    color_tags: (html.match(/color:/g) || []).length,
    sections: b.querySelectorAll('section').length,
    head: (b.innerText || '').slice(0, 60)
  };
})()""" % BODY

FOCUS_SELECT_JS = """(function(){
  var b = document.querySelector('%s');
  if (!b) return {ok:false};
  b.scrollIntoView({block:'center'});
  b.focus();
  var s = window.getSelection(), r = document.createRange();
  r.selectNodeContents(b);
  s.removeAllRanges(); s.addRange(r);
  return {ok: document.activeElement === b, selected: s.toString().length};
})()""" % BODY


def cdp(method, params=None, timeout=60):
    body = json.dumps({"method": method, "params": params or {}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + "/cdp", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())
    if not out.get("ok"):
        raise RuntimeError("/cdp 失败：%s" % str(out)[:300])
    return out.get("result") or {}


def ev(expr):
    r = cdp("Runtime.evaluate", {"expression": expr, "returnByValue": True, "userGesture": True})
    if r.get("exceptionDetails"):
        raise RuntimeError("JS 异常：" + str(r["exceptionDetails"])[:300])
    return (r.get("result") or {}).get("value")


def key(key_, code, vk, modifiers=0, text=None):
    base = {"key": key_, "code": code, "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk, "modifiers": modifiers}
    down = dict(base)
    if text:
        down["text"] = text
    cdp("Input.dispatchKeyEvent", dict(down, type="keyDown" if text else "rawKeyDown"))
    cdp("Input.dispatchKeyEvent", dict(base, type="keyUp"))


def focus_app_window():
    """把 MpLoginProbe 窗口顶到前台。

    必须做这一步：Chromium 读剪贴板要求窗口处于前台焦点，
    后台窗口发 Ctrl+V 会被直接丢弃（这就是第一次粘贴毫无反应的原因）。
    """
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetProcessDPIAware()
    hits = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if not user32.IsWindowVisible(h):
            return True
        n = user32.GetWindowTextLengthW(h)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        if "MpLoginProbe" in buf.value:
            hits.append((h, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    if not hits:
        return None
    h, title = hits[0]
    if user32.IsIconic(h):
        user32.ShowWindow(h, 9)          # SW_RESTORE
    user32.SetForegroundWindow(h)
    time.sleep(0.6)
    return title


def paste():
    """发整整一组 Ctrl+V，并同时带上 editing command。

    只发键序列不够可靠：Chromium 的粘贴是 browser 侧编辑命令，
    用 CDP 的 commands 字段直接点名 'paste'，才不依赖快捷键映射。
    """
    cdp("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": "Control", "code": "ControlLeft",
                                   "windowsVirtualKeyCode": 17, "nativeVirtualKeyCode": 17,
                                   "modifiers": 2})
    cdp("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": "v", "code": "KeyV",
                                   "windowsVirtualKeyCode": 86, "nativeVirtualKeyCode": 86,
                                   "modifiers": 2, "commands": ["paste"]})
    cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": "v", "code": "KeyV",
                                   "windowsVirtualKeyCode": 86, "nativeVirtualKeyCode": 86,
                                   "modifiers": 2})
    cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Control", "code": "ControlLeft",
                                   "windowsVirtualKeyCode": 17, "nativeVirtualKeyCode": 17,
                                   "modifiers": 0})


def insert_html(html):
    """兜底：选中状态下发 insertHTML 编辑命令（不碰剪贴板）。"""
    js = "document.execCommand('insertHTML', false, %s)" % json.dumps(html)
    return ev(js)


def save_draft():
    js = """(function(){
      var bs = Array.prototype.slice.call(document.querySelectorAll('button'));
      var b = bs.find(function(e){
        var t = (e.innerText || '').trim();
        if (!/保存/.test(t)) return false;
        if (/发表|群发|发布|推送/.test(t)) return false;
        var r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      });
      if (!b) return null;
      b.scrollIntoView({block:'center'});
      var r = b.getBoundingClientRect();
      return {x: r.x + r.width / 2, y: r.y + r.height / 2, text: (b.innerText || '').trim()};
    })()"""
    p = ev(js)
    if not p:
        return {"ok": False, "why": "没找到可见的「保存」按钮"}
    cdp("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": p["x"], "y": p["y"]})
    cdp("Input.dispatchMouseEvent", {"type": "mousePressed", "x": p["x"], "y": p["y"],
                                     "button": "left", "clickCount": 1})
    cdp("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": p["x"], "y": p["y"],
                                     "button": "left", "clickCount": 1})
    return {"ok": True, "button": p["text"]}


def main():
    dry = "--dry" in sys.argv
    do_save = "--save" in sys.argv
    frag_path = ""
    if "--frag" in sys.argv:
        frag_path = sys.argv[sys.argv.index("--frag") + 1]

    before = ev(STATE_JS)
    print("粘贴前：", json.dumps(before, ensure_ascii=False))
    if dry:
        return 0
    if not before or not before.get("ok"):
        print("❌ 正文编辑器不在页面上")
        return 1

    w = focus_app_window()
    print("探针窗口：", w or "（没找到，继续试）")

    sel = ev(FOCUS_SELECT_JS)
    print("聚焦全选：", json.dumps(sel, ensure_ascii=False))
    if not sel or not sel.get("ok"):
        print("❌ 正文编辑器拿不到焦点")
        return 1
    time.sleep(0.3)

    paste()
    print("已发 Ctrl+V（带 paste 编辑命令），等待合成…")
    time.sleep(3.0)
    after = ev(STATE_JS)
    print("粘贴后：", json.dumps(after, ensure_ascii=False))

    richer = (after["style_tags"] > before.get("style_tags", 0)
              or after["color_tags"] > before.get("color_tags", 0))
    if not richer and frag_path:
        print("⚠️ 剪贴板粘贴没进来，改用 insertHTML 兜底…")
        ev(FOCUS_SELECT_JS)
        time.sleep(0.3)
        import io
        html = io.open(frag_path, encoding="utf-8").read()
        print("insertHTML 返回：", insert_html(html))
        time.sleep(3.0)
        after = ev(STATE_JS)
        print("兜底后：", json.dumps(after, ensure_ascii=False))
        richer = (after["style_tags"] > before.get("style_tags", 0)
                  or after["color_tags"] > before.get("color_tags", 0))

    if not richer:
        print("⚠️ 样式仍然没有增加：正文可能只有纯文本")
    if after["text_len"] < 100:
        print("❌ 正文几乎空了，别保存，先手工看现场")
        return 1

    if do_save:
        r = save_draft()
        print("保存：", json.dumps(r, ensure_ascii=False))
        time.sleep(2.5)
        print("保存后：", json.dumps(ev(STATE_JS), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
