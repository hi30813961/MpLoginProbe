# -*- coding: utf-8 -*-
"""修改【已经存进公众号草稿箱】的文章（本地素材为准，覆盖后台那篇）。

背景（2026-11-12）：
  * `draft` 命令每次都打开 isNew=1 的新编辑器，重复跑会多出一篇草稿，
    所以它不适合改稿；`inject` 只补图/封面，不动标题正文。
  * 本模块按 appmsgid 打开【已有草稿】，用本地最新内容覆盖
    标题 / 作者 / 摘要 / 正文，再差量重插正文图、按需换封面，
    最后点「保存为草稿」并刷新复核。全程不新建草稿、不群发、不删素材。

目标草稿的定位顺序：
  1) --appmsgid 手动指定
  2) state.json 台账里记的 appmsgid
  3) 只读扫草稿箱列表，按标题精确匹配（卡片上的 data-appid 就是 appmsgid）

写入通道沿用已验证的两条：
  * 文字：ProseMirror 只认 CDP Input.insertText（全选后替换）
  * 图片/封面：走编辑器官方「图片库」UI，微信自己写进 PM state
  正文全选重写会连带清掉旧插图，所以之后必须再跑一次差量插图。

安全：只写自己的草稿、只点「保存为草稿」；目标 appmsgid 必须能在
草稿箱列表里找到（--current 除外）；不碰发表/群发/删除。
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(BASE))

import compose          # noqa: E402
import deliver          # noqa: E402
import media_inject     # noqa: E402
import sources          # noqa: E402
from cdp_vision import CdpError  # noqa: E402
from contract import State, STATES  # noqa: E402

CACHE = os.path.join(BASE, "drafts_cache.json")
LIST_PATH = ("https://mp.weixin.qq.com/cgi-bin/appmsg?begin=0&count=20"
             "&type=77&action=list_card&token=%s&lang=zh_CN")
BODY = "#ueditor_0 .ProseMirror"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _mat(cfg, mid):
    for m in sources.load(cfg["source"]):
        if str(m.id) == str(mid):
            return m
    raise ValueError("找不到素材 id=%s（先跑 scan 看列表）" % mid)


def _plain(fragment):
    return re.sub(r"\s+", " ",
                  html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


# ── 只读：草稿箱列表 ─────────────────────────────────────────────────────
DRAFTS_JS = r"""(function () {
  return Array.prototype.map.call(
    document.querySelectorAll('.publish_card_container'), function (e) {
      var card = e.querySelector('[data-appid]');
      var t = e.querySelector('.weui-desktop-publish__cover__title');
      var thumb = e.querySelector('.weui-desktop-publish__cover__thumb');
      var st = thumb ? (thumb.getAttribute('style') || '') : '';
      var m = st.match(/url\((?:"|')?([^"')]+)/);
      var up = (e.innerText || '').match(/更新于\s*(\S+)/);
      return {appmsgid: card ? (card.getAttribute('data-appid') || '') : '',
              title: t ? (t.innerText || '').trim() : '',
              cover: m ? m[1] : '', updated: up ? up[1] : ''};
    }).filter(function (x) { return /^\d{6,}$/.test(x.appmsgid); });
})()"""


def read_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            d = json.load(f)
        return {"items": d.get("items") or [], "source": "cache",
                "scanned_at": d.get("scanned_at", "")}
    except (OSError, ValueError):
        return {"items": [], "source": "cache", "scanned_at": ""}


def _write_cache(items):
    try:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump({"scanned_at": _now(), "items": items},
                      f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _wait(cdp, needle, rounds=40):
    for _ in range(rounds):
        time.sleep(0.5)
        try:
            if needle in str(cdp.status().get("url", "")) and deliver.ev(
                    cdp, "document.readyState") == "complete":
                return True
        except CdpError:
            pass                      # 上下文销毁中，下一轮再问
    return False


def scan_drafts(cdp, navigate=True):
    """只读扫草稿箱。当前不在列表页且 navigate=False 时直接用缓存。"""
    if "action=list_card" not in str(cdp.status().get("url", "")):
        if not navigate:
            return read_cache()
        url = LIST_PATH % deliver.token_of(cdp)
        if not cdp.cdp("Page.navigate", {"url": url}).get("ok"):
            raise CdpError("打开草稿箱失败")
        if not _wait(cdp, "action=list_card"):
            raise CdpError("草稿箱加载超时")
        time.sleep(0.8)
    items = deliver.ev(cdp, DRAFTS_JS) or []
    _write_cache(items)
    return {"ok": True, "items": items, "source": "live", "scanned_at": _now()}


def link_state(cfg, drafts):
    """把台账里『缺 appmsgid、但标题能对上后台草稿』的记录补登记。"""
    st = State()
    linked = []
    for m in sources.load(cfg["source"]):
        mid = str(m.id)
        if mid not in st.data["items"]:
            continue                        # 没台账的不擅自改成 drafted
        if st.get(mid).get("appmsgid"):
            continue
        for d in drafts or []:
            if (d.get("title") or "").strip() == (m.title or "").strip():
                cur = st.get(mid).get("status")
                st.set(mid, cur if cur in STATES else "drafted",
                       appmsgid=d["appmsgid"])
                linked.append({"id": mid, "title": m.title,
                               "appmsgid": d["appmsgid"]})
                break
    return linked


# ── 只读：改稿计划 ───────────────────────────────────────────────────────
def resolve(mid, title, explicit=None, drafts=None):
    """返回 (appmsgid, 来源)。不写任何东西。"""
    if explicit and str(explicit).isdigit():
        return str(explicit), "手动指定"
    x = State().get(mid).get("appmsgid")
    if x and str(x).isdigit():
        return str(x), "台账"
    t = (title or "").strip()
    for d in drafts or []:
        if (d.get("title") or "").strip() == t and d.get("appmsgid"):
            return str(d["appmsgid"]), "按标题匹配"
    return "", ""


def plan(cfg, mid, appmsgid=None, drafts=None, replace_cover=False,
         no_media=False):
    """只读：列出这次改稿会覆盖哪些字段、目标是哪篇后台草稿。"""
    m = _mat(cfg, mid)
    res = compose.render(m, cfg.get("compose", {}))
    mf, mp = res["manifest"], media_inject.plan(cfg, mid)
    st = State().get(mid)
    if drafts is None:
        drafts = read_cache()["items"]
    target, src = resolve(mid, m.title, appmsgid, drafts)
    plain = _plain(res["fragment"])
    n_img = len([x for x in mp["body_images"] if x["state"] == "ready"])
    fields = [
        {"key": "title", "label": "标题", "value": m.title},
        {"key": "body", "label": "正文", "value": "纯文本 %d 字 · 覆盖后台全文" % len(plain)},
        {"key": "author", "label": "作者", "value": m.author or "（空）"},
        {"key": "digest", "label": "摘要", "value": (m.summary or "（空）")[:60]},
        {"key": "body_images", "label": "正文图片",
         "value": ("保留后台原有图片（--no-media）" if no_media else
                   "按本地清单重插 %d 张（旧的会被清掉）" % n_img)},
        {"key": "cover", "label": "封面",
         "value": ("保持后台现有封面" if no_media else
                   ("强制换成 %s" % mp["cover"]["key"][:16] if replace_cover
                    else "保留后台现有封面（可勾选更换）"))},
    ]
    warnings = list(mp["warnings"])
    if mf.get("has_blocker"):
        warnings.append("本地还有待填项，默认拒绝改稿；确认无误可加 --force")
    if not target:
        warnings.append("还没定位到后台草稿；执行时会自动按标题查找，也可手填 appmsgid")
    elif not any(str(d.get("appmsgid")) == target for d in drafts):
        warnings.append("目标 appmsgid=%s 不在最近一次草稿箱列表里，执行时会重新核对" % target)
    return {"ok": True, "id": str(mid), "title": m.title,
            "appmsgid": target, "appmsgid_source": src,
            "status": st.get("status"), "last_drafted": st.get("updated_at", ""),
            "fragment_len": len(res["fragment"]), "plain_len": len(plain),
            "has_blocker": bool(mf.get("has_blocker")),
            "checks": list(mf.get("checks") or []),
            "fields": fields, "body_images": mp["body_images"],
            "cover": mp["cover"], "drafts": drafts,
            "media_blocked": bool(not no_media and any(
                x["state"] != "ready" for x in mp["body_images"])),
            "warnings": warnings,
            "ready": bool(target) and not mf.get("has_blocker")}


# ── 主流程 ──────────────────────────────────────────────────────────────
def revise(cfg, cdp, mid, *, appmsgid=None, replace_cover=False,
           no_media=False, verify_reload=True, dry_run=False,
           force=False, current=False):
    """用本地素材覆盖更新公众号里【已有】的那篇草稿。"""
    m = _mat(cfg, mid)
    res = compose.render(m, cfg.get("compose", {}))
    mp = media_inject.plan(cfg, mid)
    p = plan(cfg, mid, appmsgid=appmsgid, replace_cover=replace_cover,
             no_media=no_media)
    if p["has_blocker"] and not force:
        raise CdpError("存在待填项，先在本地核对；确认无误加 --force：\n  "
                       + "\n  ".join(p["checks"]))
    if dry_run:
        return {"ok": True, "dry_run": True, "id": str(mid), "plan": p,
                "steps": ["只读预览：未导航 / 未输入 / 未点击 / 未保存"]}
    # 前置闸门：正文重写会清掉所有旧图，本地图没上传就别动 DOM
    if not no_media and any(x["state"] != "ready" for x in mp["body_images"]):
        raise CdpError("有正文图还没上传到官方素材库，先跑 "
                       "python pipeline.py upload --id %s；"
                       "或加 --no-media 只改文字并保留后台原有图片" % mid)
    if "LOGGED_IN" not in str(cdp.status().get("state", "")).upper():
        raise CdpError("公众号未登录")

    steps = []
    if current:
        u = str(cdp.status().get("url", ""))
        if "appmsg_edit" not in u:
            raise CdpError("--current 要求探针当前就停在该草稿的编辑器页")
        mm = re.search(r"appmsgid=(\d+)", u)
        target = (str(appmsgid) if appmsgid and str(appmsgid).isdigit()
                  else (mm.group(1) if mm else resolve(mid, m.title, drafts=read_cache()["items"])[0]))
        src = "当前编辑器"
        drafts = read_cache()["items"]
    else:
        scan = scan_drafts(cdp, navigate=True)
        drafts = scan["items"]
        target, src = resolve(mid, m.title, appmsgid, drafts)
        if not target:
            raise CdpError("找不到对应的后台草稿。用 --appmsgid 指定，"
                           "或先跑 python pipeline.py drafts 看列表")
        if not any(str(d.get("appmsgid")) == target for d in drafts):
            raise CdpError("目标 appmsgid=%s 不在草稿箱列表里，拒绝写入" % target)
    if not target or not str(target).isdigit():
        raise CdpError("appmsgid 无效：%r" % target)
    steps.append("只读核对草稿箱，定位 %s（%s）" % (target, src))

    # 1) 打开【已有】草稿，不是 isNew=1
    if not current:
        media_inject.navigate_draft(cdp, target)
    time.sleep(1.0)
    steps.append("打开已有草稿 %s" % target)

    before = deliver.read_editor(cdp)
    s0 = media_inject._read_state(cdp)
    before = {"title": before.get("title_pm") or before.get("title_mirror") or "",
              "body_len": before.get("body_html_len") or 0,
              "body_text": (before.get("body_text") or "")[:60],
              "author": before.get("author") or "",
              "digest": before.get("digest") or "",
              "body_imgs": len(s0["body_imgs"]),
              "cover": (s0.get("cover_url") or "")[:160]}

    # 2) 覆盖文字（全选后 insertText，会清掉旧插图）
    deliver.fill(cdp, res["fragment"], m.title, m.author, m.summary,
                 min_body_len=1)
    steps.append("覆盖标题 / 正文 / 作者 / 摘要")

    # 3) 重插正文图 + 按需换封面（不在这里保存）
    #    第 2 步全文重写会连同旧 <img> 一起清掉，所以这里必须把图插回来。
    media = {"skipped": True}
    if no_media:
        # 只改文字：把后台原有的图原样插回，封面保持不动
        preserve = [media_inject.img_key(u) for u in s0["body_imgs"]]
        preserve = [k for k in preserve if k]
        if preserve:
            media = media_inject.inject(cfg, cdp, mid, current=True,
                                        do_cover=False, do_body=True,
                                        save=False, verify_reload=False,
                                        keys_override=preserve)
            steps.append("保留后台原有 %d 张正文图，封面不动" % len(preserve))
        else:
            steps.append("后台原本没有正文图，封面不动")
    elif mp["body_images"] or mp["cover"]["key"]:
        media = media_inject.inject(cfg, cdp, mid, current=True,
                                    do_cover=True, do_body=True, save=False,
                                    verify_reload=False,
                                    replace_cover=replace_cover)
        steps.append("正文图 %s / 封面 %s"
                     % (media["body_images"].get("action"),
                        media["cover"].get("action")))
    else:
        steps.append("本地没有图片，跳过插图")

    # 4) 只点「保存为草稿」
    sv = deliver.save_draft(cdp)
    time.sleep(2.0)
    steps.append("保存为草稿（未点发表 / 群发）")

    # 5) 刷新复核，确认服务器真的落盘
    if verify_reload:
        cdp.cdp("Page.reload", {"ignoreCache": False})
        for _ in range(40):
            time.sleep(0.5)
            try:
                if deliver.ev(cdp, "document.readyState") == "complete" and deliver.ev(
                        cdp, "!!document.querySelector(%s)" % json.dumps(BODY)):
                    break
            except CdpError:
                pass
        time.sleep(1.2)
        steps.append("刷新页面复核持久化")
    aft = deliver.read_editor(cdp)
    s1 = media_inject._read_state(cdp)
    after = {"title": aft.get("title_pm") or aft.get("title_mirror") or "",
             "body_len": aft.get("body_html_len") or 0,
             "body_text": (aft.get("body_text") or "")[:60],
             "author": aft.get("author") or "",
             "digest": aft.get("digest") or "",
             "body_imgs": len(s1["body_imgs"]),
             "cover": (s1.get("cover_url") or "")[:160]}

    # 6) 回写台账（appmsgid 不变，不新增草稿）
    State().set(str(mid), "drafted", appmsgid=target, title=m.title,
                fragment_len=len(res["fragment"]), editor=BODY,
                body_imgs=after["body_imgs"],
                cover_url=(s1.get("cover_url") or "")[:200] or None,
                revised_at=_now())
    return {"ok": True, "id": str(mid), "appmsgid": target,
            "appmsgid_source": src, "steps": steps,
            "before": before, "after": after,
            "media": {"body": media.get("body_images", {}),
                      "cover": media.get("cover", {})},
            "saved": bool(sv.get("clicked")),
            "verified": bool(verify_reload),
            "message": "已更新草稿 %s（未新建、未发表）" % target}
