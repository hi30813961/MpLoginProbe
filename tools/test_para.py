# -*- coding: utf-8 -*-
"""实验：在正文编辑器里测 CDP text + Enter 键能否分段。"""
import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pipeline"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from cdp_vision import Cdp  # noqa: E402
import deliver  # noqa: E402

cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pipeline", "config.json"), encoding="utf-8"))
c = Cdp((cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765"))

f = deliver.ev(c, deliver.FOCUS_BODY_JS)
print("focus:", f)
if not f or not f.get("sameNode"):
    print("!! 编辑器不可用")
    sys.exit(1)

# 先全选删掉现有内容（实验页，稍后会重填正文）
c.key("Ctrl+a")
time.sleep(0.2)
c.key("Backspace")
time.sleep(0.3)

# 实验：text + Enter x2 + text
c.text("甲段END")
time.sleep(0.2)
c.key("Enter"); time.sleep(0.15)
c.key("Enter"); time.sleep(0.15)
c.text("乙段END")
time.sleep(0.2)
c.key("Enter"); time.sleep(0.15)
c.text("丙段END")
time.sleep(0.4)

st = deliver.read_editor(c)
print("body_html_len:", st["body_html_len"])
print("body_text(前120):", st["body_text"])
# 数 <p> 数量与换行
n_p = deliver.ev(c, "(function(){var b=document.querySelector('#ueditor_0 .ProseMirror');return b?b.querySelectorAll(':scope > p').length:0})()")
print("顶层<p>数:", n_p)
html_head = deliver.ev(c, "(function(){var b=document.querySelector('#ueditor_0 .ProseMirror');return b?b.innerHTML.slice(0,300):''})()")
print("html头300:", html_head)
