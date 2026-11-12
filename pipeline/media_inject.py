# -*- coding: utf-8 -*-
"""把素材里的【封面 + 正文图片】真正写进公众号草稿。

背景（2026-11-12 实测）：
  * deliver.fill() 只能靠 CDP Input.insertText 写 ProseMirror 纯文本，
    合成 paste / insertHTML / CF_HTML 都只改 DOM、保存即丢。
  * 但走编辑器【官方 UI】的图片按钮，微信自己会把 <img> 写进 PM state，
    保存草稿 -> 刷新页面后图片仍在（本模块验证过）。
  * 封面同理：点「拖拽或选择封面」→ 从图片库选择 → 下一步 → 确认，
    微信会裁剪出一张新的 jpg 并持久化。

链路（全 CDP，无感化，不占真实鼠标）：
  1. 临时放大 CDP 视口到 1400x1400。探针窗口只有 1252x504，
     弹窗底栏按钮在 y≈677/848/956，不放大根本点不到。
  2. 正文：光标置末尾 → #js_editor_insertimage → 「从图片库选择」
     → 按 CDN 图片 ID 多选 → 「确定」，一次插入全部正文图。
  3. 封面：.select-cover__btn → a.js_imagedialog → 选 cover →
     「下一步」→ 裁剪页「确认」。
  4. 保存为草稿（绝不点发表/群发），刷新页面复核持久化。

幂等：正文按图片 ID 差量插入，已存在的不会重复插；封面已存在时跳过
（replace_cover=True 才替换）。图片必须是已上传到官方素材库的微信 CDN 地址，
本地路径会被拦下并提示先跑 `upload`。

安全：只写自己的草稿、只插图/封面、不删官方素材、不群发；
所有点击都限定在挑选器/工具栏，按钮文字精确匹配且带禁用词检查。
"""
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(BASE))

import sources          # noqa: E402
from cdp_vision import CdpError  # noqa: E402
from deliver import ev, token_of, click_css, save_draft, FOCUS_BODY_JS  # noqa: E402

# 视口高度必须大于图片选择弹窗底栏按钮的 y 坐标。素材库图片变多时弹窗会被撑高，
# 1000 曾让「确定」落在 y≈1011 而点不到（表现为「图片选择弹窗未关闭超时」）。
VIEWPORT = {"width": 1400, "height": 1400, "deviceScaleFactor": 1, "mobile": False}
COVER_BTN = ".select-cover__btn.js_cover_btn_area"
COVER_LIB = "a.js_imagedialog"
IMG_BTN = "#js_editor_insertimage"
IMG_LIB = ".js_img_from_local"
PICKER = ".weui-desktop-dialog_img-picker"
THUMB = ".weui-desktop-img-picker__img-thumb"
BODY = "#ueditor_0 .ProseMirror"
COVER_PREVIEW = ".js_cover_preview_new"
FORBIDDEN = ("发表", "群发", "发布", "推送", "删除", "退出登录")


# ── 基础工具 ────────────────────────────────────────────────────────────
def img_key(url):
    """mmbiz.qpic.cn/<类型>/<图片ID>/<尺寸>?... -> <图片ID>。

    素材库里是 /640 或 /300，插图后是 /0，封面裁剪后是另一张 jpg，
    所以只能靠这段 ID 在不同尺寸的 URL 之间认同一张图。
    """
    m = re.search(r"mmbiz\.qpic\.cn/[^/]+/([A-Za-z0-9_-]{12,})/", str(url or ""))
    return m.group(1) if m else ""


def _raw_meta(cfg, mid):
    p = os.path.join(os.path.join(sources._abs(cfg["source"]["path"]), str(mid)),
                     "meta.json")
    if not os.path.isfile(p):
        raise ValueError("找不到素材 %s 的 meta.json" % mid)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _title(cfg, mid, fallback=""):
    for x in sources.load(cfg["source"]):
        if str(x.id) == str(mid):
            return x.title
    return fallback


def plan(cfg, mid):
    """只读：列出这篇文章的封面 / 正文图片，以及是否已具备写入条件。"""
    raw = _raw_meta(cfg, mid)
    cover = str(raw.get("cover") or "")
    images = [str(x) for x in (raw.get("images") or []) if x]
    items = []
    for i, u in enumerate(images):
        k = img_key(u)
        items.append({"index": i, "url": u, "key": k,
                      "state": "ready" if k else ("local" if not u.startswith("http") else "unknown"),
                      "name": os.path.basename(u.split("?")[0])[:40]})
    ck, cstate = img_key(cover), "none"
    if cover:
        cstate = "ready" if ck else ("local" if not cover.startswith("http") else "unknown")
    warnings = []
    if cover and cstate != "ready":
        warnings.append("封面不是已上传的微信图片地址：%s（先跑 upload）" % cover[:80])
    bad = [x for x in items if x["state"] != "ready"]
    if bad:
        warnings.append("有 %d 张正文图不是微信 CDN 地址（先跑 upload）" % len(bad))
    return {"ok": True, "id": str(mid), "title": _title(cfg, mid, raw.get("title", "")),
            "cover": {"url": cover, "key": ck, "state": cstate},
            "body_images": items, "warnings": warnings,
            "ready": bool(ck or not cover) and not bad}


def _click(cdp, selector, label=""):
    """CDP 真实点击（CSS 像素坐标）。按钮必须可见，否则报错。"""
    p = ev(cdp, """(function(s){var e=document.querySelector(s);if(!e)return null;
      var r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2,
      w:Math.round(r.width),h:Math.round(r.height)};})(%s)""" % json.dumps(selector))
    if not p or p["w"] <= 0 or p["h"] <= 0:
        raise CdpError("控件不可见或不存在：%s（%s）" % (selector, label))
    click_css(cdp, p["x"], p["y"])
    return p


def _js_click(cdp, selector):
    """页面内合成点击：用于 hover 才显示的菜单（真实鼠标移过去弹层会收起）。"""
    r = ev(cdp, """(function(s){var e=document.querySelector(s);if(!e)return null;
      e.click();return true;})(%s)""" % json.dumps(selector))
    if not r:
        raise CdpError("页面内点击失败：%s" % selector)
    return True


def _dlg_button(cdp, text, timeout=10):
    """在【当前可见】的弹窗底栏里找按钮并真实点击。带危险词拦截。"""
    if any(w in text for w in FORBIDDEN):
        raise CdpError("拒绝点击危险按钮：%s" % text)
    expr = """(function(t){
      var bs=Array.from(document.querySelectorAll('.weui-desktop-dialog__ft button'));
      var b=bs.find(function(e){
        if((e.innerText||'').trim()!==t) return false;
        if(getComputedStyle(e).display==='none') return false;
        if(getComputedStyle(e.parentElement).display==='none') return false;
        var r=e.getBoundingClientRect(); return r.width>0 && r.height>0;
      });
      if(!b) return null;
      var r=b.getBoundingClientRect();
      return {x:r.x+r.width/2,y:r.y+r.height/2,cls:b.className,text:(b.innerText||'').trim()};
    })(%s)""" % json.dumps(text, ensure_ascii=False)
    end = time.time() + timeout
    while time.time() < end:
        p = ev(cdp, expr)
        if p:
            if any(w in p["text"] for w in FORBIDDEN):
                raise CdpError("拒绝点击危险按钮：%s" % p["text"])
            click_css(cdp, p["x"], p["y"])
            return p
        time.sleep(0.4)
    raise CdpError("等不到弹窗按钮「%s」" % text)


def _wait_picker(cdp, timeout=12, gone=False):
    end = time.time() + timeout
    while time.time() < end:
        n = ev(cdp, """(function(){var e=document.querySelector(%s);
          if(!e)return 0;var r=e.getBoundingClientRect();
          return (r.width>300&&r.height>200)?1:0;})()""" % json.dumps(PICKER))
        if (not n) if gone else n:
            return True
        time.sleep(0.4)
    raise CdpError("图片选择弹窗%s超时" % ("未关闭" if gone else "未出现"))


def _select_thumbs(cdp, keys):
    """按 CDN 图片 ID 多选。当前页找不到时切到「最近使用」再找一次。"""
    js = """(function(keys){
      var ts=Array.from(document.querySelectorAll(%s));
      var hit=[];
      keys.forEach(function(k){
        var t=ts.find(function(e){return (e.getAttribute('style')||'').indexOf(k)>=0;});
        if(t){var it=t.closest('.weui-desktop-img-picker__item')||t;it.click();hit.push(k);}
      });
      return hit;
    })(%s)"""
    js = js % (json.dumps(THUMB), "%s")
    for attempt in range(2):
        found = ev(cdp, js % json.dumps(keys)) or []
        if len(found) == len(keys):
            return found
        if attempt == 0:
            ev(cdp, """(function(){var a=Array.from(document.querySelectorAll(
              '.weui-desktop-menu__item a')).find(function(e){
                return (e.innerText||'').indexOf('最近使用')>=0;});
              if(a)a.click();return !!a;})()""")
            time.sleep(1.0)
    raise CdpError("图片库当前页/最近使用里找不到目标图片：%s" % keys)


def _read_state(cdp):
    return ev(cdp, r"""(function(){
      var b=document.querySelector('#ueditor_0 .ProseMirror');
      var imgs=b?Array.from(b.querySelectorAll('img')).map(function(i){return i.src;}):[];
      var cv=document.querySelector('.js_cover_preview_new');
      var st=cv?(cv.getAttribute('style')||''):'';
      var m=st.match(/url\((?:"|')?([^"')]+)/);
      return {url:location.href, body_len:b?b.innerHTML.length:0,
              body_imgs:imgs.filter(Boolean), cover_url:m?m[1]:'',
              cover_visible:cv?getComputedStyle(cv).display:''};
    })()""")


def _focus_end(cdp):
    return ev(cdp, r"""(function(){
      var e=document.querySelector('#ueditor_0 .ProseMirror'); if(!e)return null;
      e.focus(); var s=window.getSelection(), r=document.createRange();
      r.selectNodeContents(e); r.collapse(false); s.removeAllRanges(); s.addRange(r);
      return document.activeElement===e;
    })()""")


def navigate_draft(cdp, appmsgid):
    """打开已有草稿的编辑器（只导航，不改内容）。"""
    t = token_of(cdp)
    url = ("https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit"
           "&action=edit&type=77&appmsgid=%s&token=%s&lang=zh_CN" % (appmsgid, t))
    if not cdp.cdp("Page.navigate", {"url": url}).get("ok"):
        raise CdpError("打开草稿 %s 失败" % appmsgid)
    for _ in range(40):
        time.sleep(0.5)
        try:
            if ev(cdp, "document.readyState") == "complete" and ev(
                    cdp, "!!document.querySelector('#ueditor_0 .ProseMirror')"):
                return url
        except CdpError:
            pass
    raise CdpError("草稿编辑器加载超时")


# ── 主流程 ──────────────────────────────────────────────────────────────
def inject(cfg, cdp, mid, *, appmsgid=None, current=False,
           do_cover=True, do_body=True, save=True,
           replace_cover=False, reset_body=False,
           verify_reload=True, dry_run=False, keys_override=None):
    """给草稿补正文图片 / 封面（或新建草稿时直接带上）。

    appmsgid  -> 导航到该草稿再补；current=True -> 用探针当前打开的编辑器；
    都不给    -> 调用方已经导航好（例如 draft 刚新建的编辑器）。
    keys_override -> 完全替代本地清单里的目标图片 ID（revise --no-media
                     用它把后台原有图片原样插回去，避免重写正文后丢图）。
    """
    p = plan(cfg, mid)
    target_keys = [x["key"] for x in p["body_images"] if x["state"] == "ready"]
    if keys_override is not None:
        target_keys = [k for k in keys_override if k]
    cover_key = p["cover"]["key"]
    result = {"ok": True, "id": str(mid), "title": p["title"],
              "dry_run": bool(dry_run), "steps": [], "warnings": list(p["warnings"]),
              "cover": {"action": "skip"}, "body_images": {"action": "skip"}}
    if dry_run:
        result["steps"] = ["只读预览：不导航 / 不点击 / 不保存"]
        result["plan"] = {"body_keys": target_keys, "cover_key": cover_key,
                          "viewport": VIEWPORT}
        return result
    if p["warnings"] and (not target_keys and not cover_key):
        raise CdpError("图片还没上传到官方素材库，先跑：python pipeline.py upload --id %s" % mid)
    if "LOGGED_IN" not in str(cdp.status().get("state", "")).upper():
        raise CdpError("公众号未登录")

    overridden = False
    try:
        # 1) 定位编辑器 + 放大视口（弹窗底栏按钮在 504 高的窗口外）
        if appmsgid:
            navigate_draft(cdp, appmsgid)
            result["steps"].append("打开草稿 %s" % appmsgid)
        elif not current:
            # 调用方已导航好，这里只确认编辑器在
            pass
        cdp.cdp("Emulation.setDeviceMetricsOverride", VIEWPORT)
        overridden = True
        time.sleep(0.8)
        result["steps"].append("临时放大 CDP 视口 %dx%d"
                              % (VIEWPORT["width"], VIEWPORT["height"]))
        if not ev(cdp, "!!document.querySelector('#ueditor_0 .ProseMirror')"):
            raise CdpError("当前页不是图文编辑器")

        before = _read_state(cdp)

        # 2) 可选：重写正文纯文本（用于清掉旧的/错的插图）
        if reset_body:
            plain = ev(cdp, "document.querySelector('#ueditor_0 .ProseMirror').innerText")
            if not ev(cdp, FOCUS_BODY_JS):
                raise CdpError("正文没能获得焦点")
            cdp.text(re.sub(r"\s+", " ", plain or "").strip())
            time.sleep(0.5)
            result["steps"].append("重写正文纯文本，清掉已有插图")

        # 3) 正文图片（差量、一次多选插入）
        existing = set(filter(None, (img_key(u) for u in _read_state(cdp)["body_imgs"])))
        missing = [k for k in target_keys if k not in existing]
        if do_body and missing:
            if not _focus_end(cdp):
                raise CdpError("正文光标设置失败")
            _click(cdp, IMG_BTN, "图片工具栏")
            time.sleep(0.6)
            _click(cdp, IMG_LIB, "从图片库选择")
            _wait_picker(cdp)
            hit = _select_thumbs(cdp, missing)
            time.sleep(0.5)
            _dlg_button(cdp, "确定")
            _wait_picker(cdp, gone=True)
            # 等微信把 <img> 渲染进 PM
            for _ in range(20):
                time.sleep(0.5)
                cur = _read_state(cdp)
                got = set(filter(None, (img_key(u) for u in cur["body_imgs"])))
                if all(k in got for k in missing):
                    break
            result["body_images"] = {"action": "inserted", "keys": hit,
                                     "img_count": len(_read_state(cdp)["body_imgs"])}
            result["steps"].append("从图片库插入 %d 张正文图" % len(hit))
        elif do_body:
            result["body_images"] = {"action": "already",
                                     "message": "正文图片已存在，跳过（幂等）"}

        # 4) 封面（单选 -> 下一步 -> 裁剪页确认）
        cv = _read_state(cdp)
        has_cover = "mmbiz.qpic.cn" in (cv["cover_url"] or "")
        if do_cover and cover_key and (not has_cover or replace_cover):
            ev(cdp, "document.querySelector(%s).scrollIntoView({block:'center'})"
               % json.dumps(COVER_BTN))
            time.sleep(0.4)
            _click(cdp, COVER_BTN, "拖拽或选择封面")
            time.sleep(0.8)
            _js_click(cdp, COVER_LIB)          # hover 菜单，只能页面内点
            _wait_picker(cdp)
            _select_thumbs(cdp, [cover_key])
            time.sleep(0.4)
            _dlg_button(cdp, "下一步")
            time.sleep(1.2)
            _dlg_button(cdp, "确认")           # 裁剪页
            for _ in range(16):
                time.sleep(0.5)
                if "mmbiz.qpic.cn" in (_read_state(cdp)["cover_url"] or ""):
                    break
            result["cover"] = {"action": "set", "url": _read_state(cdp)["cover_url"],
                               "key": cover_key}
            result["steps"].append("设置封面（微信裁剪后新地址）")
        elif do_cover:
            result["cover"] = {"action": "already" if has_cover else "no_cover_key",
                               "url": cv["cover_url"]}

        # 5) 保存（只点「保存为草稿」，绝不碰发表/群发）
        if save:
            save_draft(cdp)
            time.sleep(2.0)
            result["steps"].append("保存为草稿")

        # 6) 刷新复核：确认图片/封面真的写进了服务器
        after = _read_state(cdp)
        if verify_reload and save:
            cdp.cdp("Page.reload", {"ignoreCache": False})
            for _ in range(40):
                time.sleep(0.5)
                try:
                    if ev(cdp, "document.readyState") == "complete" and ev(
                            cdp, "!!document.querySelector('#ueditor_0 .ProseMirror')"):
                        break
                except CdpError:
                    pass
            time.sleep(1.5)
            after = _read_state(cdp)
            result["steps"].append("刷新页面复核持久化")
        m = re.search(r"appmsgid=(\d+)", after["url"] or "")
        result.update({"appmsgid": m.group(1) if m else "",
                       "verify": {
                           "body_imgs": len(after["body_imgs"]),
                           "body_keys": list(filter(None, (img_key(u) for u in after["body_imgs"]))),
                           "cover": after["cover_url"],
                           "persisted": bool(after["body_imgs"] or "mmbiz.qpic.cn" in (after["cover_url"] or "")),
                       }})
        return result
    finally:
        if overridden:
            try:
                cdp.cdp("Emulation.clearDeviceMetricsOverride", {})
            except Exception:
                pass
