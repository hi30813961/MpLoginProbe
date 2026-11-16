# -*- coding: utf-8 -*-
"""批量复核草稿：逐篇从服务器重新打开编辑器，读回标题/正文/样式/封面。

用法： py tools/verify_batch.py 100000138 100000141 100000144 100000147
"""
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, "tools")
import paste_html as P  # noqa: E402

BASE = "http://127.0.0.1:8765"
URL = ("https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit&action=edit"
       "&type=77&appmsgid=%s&token=%s&lang=zh_CN")


def get_token():
    """登录态 token 不入库：环境变量 > 探针 /status > 当前页面 URL。"""
    t = os.environ.get("MP_TOKEN")
    if t:
        return t
    try:
        with urllib.request.urlopen(BASE + "/status", timeout=5) as r:
            s = r.read().decode("utf-8", "replace")
        m = (re.search(r'"token"\s*:\s*"?(\d{5,})', s)
             or re.search(r"token=(\d{5,})", s))
        if m:
            return m.group(1)
    except Exception:
        pass
    try:
        u = P.ev("location.href") or ""
        m = re.search(r"token=(\d{5,})", u)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None

STATE_JS = r"""(function(){
  var b = document.querySelector('#ueditor_0 .ProseMirror');
  var t = document.querySelector('.title-editor__input .ProseMirror');
  if (!b) return null;
  var blocks = Array.prototype.slice.call(b.children)
      .map(function(e){ return (e.innerText || '').trim(); });
  var cover = document.querySelector('.js_cover_preview_new');
  return {
    title: t ? t.innerText.trim() : '',
    len: b.innerText.length,
    h2: b.querySelectorAll('h2').length,
    bq: b.querySelectorAll('blockquote').length,
    styles: (b.innerHTML.match(/style=/g) || []).length,
    blocks: blocks.length,
    first: blocks[0] ? blocks[0].slice(0, 26) : '',
    last: blocks.length ? blocks[blocks.length - 1].slice(0, 26) : '',
    dups: (function(){
      var seen = {}, n = 0;
      blocks.forEach(function(x){
        if (x.length < 15) return;
        var k = x.slice(0, 15);
        if (seen[k]) n += 1;
        seen[k] = 1;
      });
      return n;
    })(),
    coverShown: !!cover && cover.getBoundingClientRect().width > 0
  };
})()"""


def wait_editor(timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = P.ev(STATE_JS)
            if isinstance(r, dict) and r.get("len", 0) > 500:
                return r
        except Exception:
            pass
        time.sleep(2)
    return None


def main():
    ids = sys.argv[1:]
    if not ids:
        print("用法： py tools/verify_batch.py <appmsgid> ...")
        print("     登录态 token 自动从探针 /status 读取；也可设 MP_TOKEN 环境变量")
        return 1
    token = get_token()
    if not token:
        print("❌ 拿不到登录态 token：请先启动探针，或设 MP_TOKEN=<token>")
        return 1
    bad = 0
    for appid in ids:
        P.cdp("Page.navigate", {"url": URL % (appid, token)})
        time.sleep(2)
        r = wait_editor()
        if not r:
            print("%s  ❌ 编辑器未就绪或正文丢失" % appid)
            bad += 1
            continue
        ok = (r["len"] > 2000 and r["h2"] >= 5 and r["styles"] >= 50
              and r["dups"] == 0)
        print("%s  %s  %s" % (appid, "✅" if ok else "❌", r["title"]))
        print("     %d 字 / %d 块 / %d 小标题 / %d 引用 / %d 样式 / 重复 %d"
              % (r["len"], r["blocks"], r["h2"], r["bq"], r["styles"], r["dups"]))
        print("     首段：%s ｜ 末段：%s" % (r["first"], r["last"]))
        if not ok:
            bad += 1
    print("\n%s  %d/%d 篇通过" % ("ALL PASS" if bad == 0 else "FAILED", len(ids) - bad, len(ids)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
