# -*- coding: utf-8 -*-
"""清空正文 -> 剪贴板粘贴排版 HTML -> 轮询验证 -> 保存。

修 bug 的关键认知（2026-11-15 实测）：
  * 微信编辑器粘贴大 HTML 后合成很慢，3 秒内读回的状态可能是旧的，
    别急着判定失败、更别急着上兜底 —— 一兜底就会插两遍。
  * 必须先 focus_app_window（Chromium 读剪贴板要求前台焦点）。
  * Ctrl+V 用 CDP 的 commands:['paste'] 直接点名编辑命令，不依赖快捷键映射。

用法：
    python tools/reflow.py --frag out/ai-compute-1115/fragment.html --save
"""
import ctypes
import ctypes.wintypes as wt
import json
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
BODY = '#ueditor_0 .ProseMirror'

STATE_JS = r"""(function(){
  var b = document.querySelector('%s');
  if (!b) return {ok:false};
  var html = b.innerHTML || '';
  return {ok:true, text_len:(b.innerText||'').length, html_len:html.length,
          style_tags:(html.match(/style="/g)||[]).length,
          headings:b.querySelectorAll('h1,h2,h3').length,
          blockquotes:b.querySelectorAll('blockquote').length};
})()""" % BODY

FOCUS_SELECT_JS = r"""(function(){
  var b = document.querySelector('%s');
  if (!b) return {ok:false};
  b.scrollIntoView({block:'center'});
  b.focus();
  var s = window.getSelection(), r = document.createRange();
  r.selectNodeContents(b);
  s.removeAllRanges(); s.addRange(r);
  return {ok: document.activeElement === b, n: s.toString().length};
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
    # cdp() 已经解包过探针的 {"ok","result"} 一层，这里 r 即 CDP 原始返回体
    if r.get("exceptionDetails"):
        raise RuntimeError("JS 异常：" + json.dumps(r["exceptionDetails"])[:200])
    return (r.get("result") or {}).get("value")


def focus_app_window():
    """把探针窗口（标题「公众号工作台 v5.1 …」）恢复并顶到前台。

    两个必须：
      1) 标题里没有 'MpLoginProbe' 字样，按 '公众号工作台' 匹配；
      2) 程序默认最小化运行（标题里自己写了'最小化省内存'），最小化时
         WebView2 挂起，Chromium 拒绝读剪贴板 —— Ctrl+V 会无声失败。
         必须先 SW_RESTORE 再 SetForegroundWindow。
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetProcessDPIAware()
    hits = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        n = user32.GetWindowTextLengthW(h)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        if "公众号工作台" in buf.value and "8765" in buf.value:
            hits.append((h, buf.value.split("  |  ")[0]))
        return True

    user32.EnumWindows(cb, 0)
    if not hits:
        return None
    h, title = hits[0]
    if user32.IsIconic(h):
        user32.ShowWindow(h, 9)          # SW_RESTORE —— 最小化状态下粘贴必败
        time.sleep(1.0)
    user32.SetForegroundWindow(h)
    time.sleep(0.6)
    return title


def sendinput_combo(vk_main, with_ctrl=True):
    """真人级按键注入：窗口必须已在前台（focus_app_window 已保证）。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetProcessDPIAware()
    KEYUP = 0x0002
    if with_ctrl:
        user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(vk_main, 0, 0, 0)
    user32.keybd_event(vk_main, 0, KEYUP, 0)
    if with_ctrl:
        user32.keybd_event(0x11, 0, KEYUP, 0)


def paste_real():
    """Ctrl+A / Ctrl+V 全部走真实键盘。

    CDP 的 commands:['paste'] 在 WebView2 里读不到剪贴板（实测静默失败），
    只有宿主窗口收到真实键盘消息时 Chromium 才会走正常粘贴管线。
    选区仍由 CDP 预先设在正文里，Ctrl+A 只是让编辑器以自己的方式再确认一次。
    """
    sel = ev(r"""(function(){
      var b = document.querySelector('%s');
      var s = window.getSelection();
      if (!b) return {ok:false};
      b.focus();
      var r = document.createRange();
      r.selectNodeContents(b);
      s.removeAllRanges(); s.addRange(r);
      return {ok: document.activeElement === b,
              selLen: s.toString().length,
              bodyLen: b.innerText.length};
    })()""" % BODY)
    print("选区准备：", json.dumps(sel, ensure_ascii=False))
    if not sel or not sel.get("ok"):
        raise RuntimeError("正文拿不到焦点，不发键盘")

    body_len = sel.get("bodyLen", 0)
    if body_len >= 100:
        # 已有内容才需要 Ctrl+A，且必须确认选区真落在正文里，防止误伤
        sendinput_combo(0x41)                 # Ctrl+A —— 编辑器内全选
        time.sleep(0.4)
        chk = ev(r"""(function(){
          var b = document.querySelector('%s');
          var s = window.getSelection();
          return {selLen: s.toString().length, bodyLen: b.innerText.length,
                  inside: b.contains(s.anchorNode)};
        })()""" % BODY)
        print("全选确认：", json.dumps(chk, ensure_ascii=False))
        if not chk or not chk.get("inside") or chk.get("selLen", 0) < 100:
            raise RuntimeError("全选范围不对（%s），放弃粘贴防误伤" % chk)
    else:
        # 正文本来就是空的（比如上一轮清空后中断），没有可误伤的内容，直接粘
        print("正文为空（%d 字符），跳过全选直接粘贴" % body_len)

    sendinput_combo(0x56)                 # Ctrl+V —— 真实粘贴
    print("已发真实 Ctrl+V，轮询等待合成…")


def backspace():
    cdp("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": "Backspace", "code": "Backspace",
                                   "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8})
    cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace",
                                   "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8})


def clear_body():
    last = None
    for i in range(12):
        try:
            ev(FOCUS_SELECT_JS)
        except RuntimeError as e:
            print("  聚焦时页面忙（DOM 重建中），稍候：", str(e)[:80])
            time.sleep(1.0)
            continue
        time.sleep(0.15)
        backspace()
        time.sleep(0.5)
        try:
            last = ev(STATE_JS)
        except RuntimeError as e:
            print("  读状态时页面忙，稍候：", str(e)[:80])
            time.sleep(1.0)
            continue
        if last is None:
            continue
        if last.get("text_len", 99) < 10 and last.get("headings", 9) == 0:
            return last
    return last


def copy_clipboard(mat_id):
    p = subprocess.run(["python", "pipeline.py", "copy", "--id", mat_id],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    ok = "已把" in (p.stdout or "")
    print("copy:", "OK" if ok else (p.stdout[-200:] + p.stderr[-200:]))
    return ok


def save_draft():
    js = r"""(function(){
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
      return {x: r.x + r.width/2, y: r.y + r.height/2, text: (b.innerText||'').trim()};
    })()"""
    p = ev(js)
    if not p:
        return {"ok": False, "why": "没找到「保存」按钮"}
    cdp("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": p["x"], "y": p["y"]})
    cdp("Input.dispatchMouseEvent", {"type": "mousePressed", "x": p["x"], "y": p["y"],
                                     "button": "left", "clickCount": 1})
    cdp("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": p["x"], "y": p["y"],
                                     "button": "left", "clickCount": 1})
    return {"ok": True, "button": p["text"]}


def main():
    frag = sys.argv[sys.argv.index("--frag") + 1] if "--frag" in sys.argv else ""
    mat_id = sys.argv[sys.argv.index("--id") + 1] if "--id" in sys.argv else ""
    do_save = "--save" in sys.argv

    print("窗口：", focus_app_window() or "（没找到，继续）")
    try:
        st = ev(STATE_JS)
    except RuntimeError as e:
        print("初读失败（可能正在自动保存），跳过：", str(e)[:80])
        st = None
    print("清空前：", json.dumps(st, ensure_ascii=False))

    cleared = clear_body()
    print("清空后：", json.dumps(cleared, ensure_ascii=False))
    if not cleared or cleared.get("text_len", 999) > 10:
        print("❌ 正文没清干净，停在这里不动保存")
        return 1

    if mat_id:
        copy_clipboard(mat_id)
    elif not frag:
        print("❌ 需要 --id（重新 copy）或确认剪贴板仍有内容")
        return 1

    ev(FOCUS_SELECT_JS)
    time.sleep(0.3)
    try:
        paste_real()
    except RuntimeError as e:
        print("❌", e)
        return 1

    target = None
    for i in range(20):
        time.sleep(1.0)
        st = ev(STATE_JS)
        print("  t+%ds" % (i + 1), json.dumps(st, ensure_ascii=False))
        if st.get("style_tags", 0) > 40 and st.get("headings", 0) > 0:
            target = st
            break
    if not target:
        print("❌ 15 秒内样式没进来，不保存")
        return 1
    if target.get("headings", 0) > 7:
        print("⚠️ 小标题超过 7 个，疑似重复插入，不保存")
        return 1

    print("✅ 排版已进入编辑器（headings=%d, style=%d）"
          % (target["headings"], target["style_tags"]))
    if do_save:
        print("保存：", json.dumps(save_draft(), ensure_ascii=False))
        time.sleep(3)
        print("保存后：", json.dumps(ev(STATE_JS), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
