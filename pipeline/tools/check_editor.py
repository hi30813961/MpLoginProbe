# -*- coding: utf-8 -*-
"""核对当前编辑器里的标题/小标题/引用块结构，并可选截图。"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8765"

JS = r"""(function(){
  var b = document.querySelector('#ueditor_0 .ProseMirror');
  if (!b) return {err:'no body'};
  var t = document.querySelector('.title-editor__input .ProseMirror');
  var out = {title: t ? t.innerText : null,
             blockquotes: b.querySelectorAll('blockquote').length,
             paras: b.children.length, headings: []};
  Array.prototype.slice.call(b.querySelectorAll('h1,h2,h3')).forEach(function(e){
    var st = e.getAttribute('style') || '';
    out.headings.push({tag: e.tagName, text: (e.innerText||'').slice(0,24),
      styled: st.length > 20, color: (st.match(/color:\s*([^;]+)/)||[])[1] || ''});
  });
  return out;
})()"""


def cdp(method, params=None, timeout=60):
    body = json.dumps({"method": method, "params": params or {}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + "/cdp", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ev(expr):
    r = cdp("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    res = (r.get("result") or {}).get("result") or {}
    return res.get("value")


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


if __name__ == "__main__":
    print(json.dumps(ev(JS), ensure_ascii=False, indent=1)[:3000])
    if "--save" in sys.argv:
        print("保存：", json.dumps(save_draft(), ensure_ascii=False))
        time.sleep(2)
