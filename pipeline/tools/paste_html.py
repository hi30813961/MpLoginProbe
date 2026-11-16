# -*- coding: utf-8 -*-
"""把排版 HTML 直接塞进微信编辑器正文（不依赖系统剪贴板）。

为什么不用剪贴板：
  * WebView2 最小化/失焦时 Chromium 拒绝读剪贴板，Ctrl+V 静默失败；
  * SetForegroundWindow 在后台进程里经常被 Windows 拦（不允许抢占前台）；
  * 结论：靠剪贴板等于赌窗口状态，工程上不可靠。

本脚本的做法（两条通道，按可靠性排序）：
  1) event  —— 在页面内构造 ClipboardEvent('paste')，clipboardData 用 DataTransfer
     塞 text/html，直接派发给 ProseMirror 根节点。PM 的 paste handler 与真人粘贴
     是同一条代码路径（不检查 isTrusted），会走 view.dispatch 更新 state，
     因此保存时不会被序列化回旧内容。
  2) insert —— document.execCommand('insertHTML')，只改 DOM 的兜底，
     必须在保存后重载校验，不通过就不算成功。

判定真假的标准只有一个：保存 → 重新打开草稿 → 内容还在。

用法：
    python tools/paste_html.py --frag out/ai-compute-1115/fragment.html --mode event
    python tools/paste_html.py --id ai-compute-1115 --mode event --save
"""
import io
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
BODY = "#ueditor_0 .ProseMirror"

STATE_JS = r"""(function(){
  var b = document.querySelector('%s');
  if (!b) return {ok:false};
  var h = b.innerHTML || '';
  return {ok:true, text_len:(b.innerText||'').length, html_len:h.length,
          style_tags:(h.match(/style="/g)||[]).length,
          headings:b.querySelectorAll('h1,h2,h3').length,
          blockquotes:b.querySelectorAll('blockquote').length,
          first:(b.innerText||'').slice(0,40)};
})()""" % BODY


def cdp(method, params=None, timeout=90):
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
        raise RuntimeError("JS 异常：" + json.dumps(r["exceptionDetails"], ensure_ascii=False)[:300])
    return (r.get("result") or {}).get("value")


def paste_event(fragment_html, plain):
    """合成 paste 事件：clipboardData 里放 HTML，交给 PM 的 paste handler。

    注意：选区必须在【上一次】JS 求值里设好、间隔 ≥300ms 再派发——
    ProseMirror 的粘贴用的是自己内部 state.selection，同一次求值里刚设的
    DOM 选区还没被它的 selectionchange 同步消费，会变成「追加」而不是替换。
    """
    js = r"""(function(){
      var b = document.querySelector('%s');
      if (!b) return {ok:false, why:'正文编辑器不在页面上'};
      var dt = new DataTransfer();
      dt.setData('text/html', HTML);
      dt.setData('text/plain', PLAIN);
      var e = new ClipboardEvent('paste', {bubbles:true, cancelable:true, clipboardData:dt});
      var notCancelled = b.dispatchEvent(e);
      return {ok:true, notCancelled:notCancelled, len:(b.innerText||'').length,
              hasDt: !!(e.clipboardData && e.clipboardData.getData('text/html').length)};
    })()""" % BODY
    js = js.replace("PLAIN", json.dumps(plain, ensure_ascii=False))
    js = js.replace("HTML", json.dumps(fragment_html, ensure_ascii=False))
    return ev(js)


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


def backspace():
    cdp("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": "Backspace", "code": "Backspace",
                                   "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8})
    cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace",
                                   "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8})


def clear_body(max_rounds=12):
    """全选 + Backspace 循环清空正文（选区与按键分两次求值，PM 能跟上）。"""
    last = None
    for _ in range(max_rounds):
        try:
            ev(FOCUS_SELECT_JS)
        except RuntimeError:
            time.sleep(1.0)
            continue
        time.sleep(0.2)
        backspace()
        time.sleep(0.6)
        try:
            last = ev(STATE_JS)
        except RuntimeError:
            time.sleep(1.0)
            continue
        if last and last.get("text_len", 99) <= 12 and last.get("headings", 9) == 0:
            return last
    return last


def paste_insert(fragment_html):
    js = r"""(function(){
      var b = document.querySelector('%s');
      if (!b) return {ok:false};
      b.focus();
      var s = window.getSelection(), r = document.createRange();
      r.selectNodeContents(b);
      s.removeAllRanges(); s.addRange(r);
      var done = document.execCommand('insertHTML', false, HTML);
      return {ok:true, done:done, len:(b.innerText||'').length};
    })()""" % BODY
    return ev(js.replace("HTML", json.dumps(fragment_html, ensure_ascii=False)))


def save_draft():
    js = r"""(function(){
      var bs = Array.prototype.slice.call(document.querySelectorAll('button'));
      var b = bs.find(function(e){
        var t = (e.innerText||'').trim();
        if (!/保存/.test(t)) return false;
        if (/发表|群发|发布|推送/.test(t)) return false;
        var r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      });
      if (!b) return null;
      b.scrollIntoView({block:'center'});
      var r = b.getBoundingClientRect();
      return {x:r.x + r.width/2, y:r.y + r.height/2, text:(b.innerText||'').trim()};
    })()"""
    p = ev(js)
    if not p:
        return {"ok": False, "why": "没找到「保存」按钮"}
    for t in ("mouseMoved", "mousePressed", "mouseReleased"):
        cdp("Input.dispatchMouseEvent", {"type": t, "x": p["x"], "y": p["y"],
                                        "button": "left", "clickCount": 1})
    return {"ok": True, "button": p["text"]}


def main():
    frag_path = ""
    mat_id = ""
    mode = "event"
    do_save = "--save" in sys.argv
    if "--frag" in sys.argv:
        frag_path = sys.argv[sys.argv.index("--frag") + 1]
    if "--id" in sys.argv:
        mat_id = sys.argv[sys.argv.index("--id") + 1]
        frag_path = frag_path or "out/%s/fragment.html" % mat_id
    if "--mode" in sys.argv:
        mode = sys.argv[sys.argv.index("--mode") + 1]
    if not frag_path:
        print("❌ 需要 --frag 或 --id")
        return 2

    frag = io.open(frag_path, encoding="utf-8").read()
    import re as _re
    plain = _re.sub(r"<[^>]+>", "", frag)
    plain = _re.sub(r"\n{3,}", "\n\n", plain).strip()
    print("片段 %d 字符 / %d 字节，模式 %s" % (len(frag), len(frag.encode("utf-8")), mode))

    before = ev(STATE_JS)
    print("粘贴前：", json.dumps(before, ensure_ascii=False))

    if before and before.get("text_len", 0) > 100:
        print("正文已有内容，先清空…")
        cleared = clear_body()
        print("清空后：", json.dumps(cleared, ensure_ascii=False))
        if not cleared or cleared.get("text_len", 999) > 100:
            print("❌ 正文没清干净，不粘贴")
            return 1

    # 选区单独一次求值，给 PM 留出 selectionchange 同步时间
    sel = ev(FOCUS_SELECT_JS)
    print("选区：", json.dumps(sel, ensure_ascii=False))
    if not sel or not sel.get("ok"):
        print("❌ 正文拿不到焦点")
        return 1
    time.sleep(0.6)

    r = paste_event(frag, plain) if mode == "event" else paste_insert(frag)
    print("派发结果：", json.dumps(r, ensure_ascii=False))
    if not r or not r.get("ok"):
        return 1

    target = None
    for i in range(15):
        time.sleep(1.0)
        st = ev(STATE_JS)
        print("  t+%ds" % (i + 1), json.dumps(st, ensure_ascii=False)[:180])
        if st.get("headings", 0) >= 5 and st.get("style_tags", 0) > 40:
            target = st
            break
    if not target:
        print("❌ 样式没进来")
        return 1
    if target.get("headings", 0) > 8:
        print("⚠️ 小标题过多（%d），疑似重复插入，不保存" % target["headings"])
        return 1

    print("✅ 排版已进入编辑器：%d 段 / %d 字 / style=%d / 引用=%d"
          % (target.get("headings", 0), target.get("text_len", 0),
             target.get("style_tags", 0), target.get("blockquotes", 0)))
    if do_save:
        print("保存：", json.dumps(save_draft(), ensure_ascii=False))
        time.sleep(4)
        print("保存后：", json.dumps(ev(STATE_JS), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
