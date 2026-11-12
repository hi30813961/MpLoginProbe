# -*- coding: utf-8 -*-
"""素材 -> 文章 -> 草稿 -> 发布 总入口。

  python pipeline.py new --id my-002 --title "新文章标题"   # 起一篇素材骨架
  python pipeline.py scan
  python pipeline.py build                 # 全部素材生成 HTML
  python pipeline.py build --id demo-001
  python pipeline.py probe                 # 只读抓编辑器 DOM（需探针在线）
  python pipeline.py draft --id demo-001 --dry-run
  python pipeline.py draft --id demo-001             # 正文 + 正文图 + 封面一起存草稿
  python pipeline.py inject --id demo-001 --dry-run # 只看图片/封面计划
  python pipeline.py inject --id demo-001           # 给已有草稿补图 / 补封面
  python pipeline.py drafts                         # 只读列出后台草稿箱 + 补登记 appmsgid
  python pipeline.py revise --id demo-001 --dry-run # 只看「改已存草稿」计划
  python pipeline.py revise --id demo-001           # 用本地内容覆盖更新【已有】那篇草稿
  python pipeline.py revise --id demo-001 --appmsgid 100000035 --replace-cover
  python pipeline.py copy --id demo-001             # 排版 HTML 放进剪贴板
  python pipeline.py upload --id demo-001 --dry-run # 只看待上传的本地图片
  python pipeline.py upload --id demo-001           # 上传公众号素材库 + 回填 CDN
  python pipeline.py upload --id demo-001 --file images/logo.png
  python pipeline.py publish --id demo-001          # 草稿模式：只给手动发布指引
  python pipeline.py published --id demo-001        # 你手动发布后，回写台账与素材系统
  python pipeline.py delete --id demo-001 --dry-run # 只看将被删除的清单
  python pipeline.py delete --id demo-001           # 移入回收站（可还原）
  python pipeline.py trash                          # 查看回收站
  python pipeline.py restore --token 20261112-204500__demo-001
  python pipeline.py purge --token xxx             # 彻底删除回收站条目
  python pipeline.py status
  python pipeline.py ui                          # 打开素材库管理界面 http://127.0.0.1:8766
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import compose          # noqa: E402
import deliver          # noqa: E402
import sources          # noqa: E402
import trash            # noqa: E402
import upload_media     # noqa: E402
import media_inject     # noqa: E402
import revise           # noqa: E402
from contract import State  # noqa: E402

DEFAULT_CFG = os.path.join(BASE, "config.json")
EXAMPLE_CFG = os.path.join(BASE, "config.example.json")


def cfg_load(path=None):
    p = path or DEFAULT_CFG
    if not os.path.exists(p):
        p = EXAMPLE_CFG
        print("⚠️ 未找到 config.json，使用 config.example.json（示例素材）")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def out_dir(cfg):
    d = (cfg.get("output") or {}).get("dir", "out")
    return d if os.path.isabs(d) else os.path.join(BASE, d)


def find(mats, mid):
    for m in mats:
        if str(m.id) == str(mid):
            return m
    raise SystemExit("❌ 找不到素材 id=%s（先跑 scan 看列表）" % mid)


def _dw(s):
    """中文按 2 列宽算，表格才不会错位。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def table(rows, head):
    widths = [max([_dw(head[i])] + [_dw(r[i]) for r in rows])
              for i in range(len(head))]
    fmt = lambda r: " | ".join(
        str(x) + " " * max(0, widths[i] - _dw(x)) for i, x in enumerate(r))
    print(fmt(head))
    print("-+-".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))


# ── 命令 ──────────────────────────────────────────────────────────────────
def cmd_new(cfg, a):
    """给本地素材库快速起一篇：生成 meta.json + body.md 骨架。"""
    title = a.title or "未命名素材"
    mid = a.id or datetime.now().strftime("%m%d-%H%M")
    root = cfg["source"]["path"]
    if not os.path.isabs(root):
        root = os.path.join(BASE, root)
    d = os.path.join(root, mid)
    meta = {
        "id": mid, "title": title, "summary": "",
        "author": (cfg.get("compose") or {}).get("default_author", ""),
        "cover": "", "images": [], "tags": [],
        "source": mid, "created_at": datetime.now().strftime("%Y-%m-%d"),
    }
    body = ("## 小标题\n\n"
            "在这里写正文。支持 **加粗**、`行内代码`、> 引用、- 列表、--- 分隔线。\n\n"
            "插图：把图片路径（或公网 URL）写进 meta.json 的 images 数组，"
            "正文用 ![图注](0) 引用第 0 张、![图注](1) 引用第 1 张。"
            "本地图片会标「待上传」，发布前需在微信素材库上传。\n\n"
            "> 价格、授权、准确率等未确认信息一律写【待填】，不要编造。\n\n"
            "---\n\n收尾信息。\n")
    if a.dry_run:
        print("✅ dry-run：将创建 %s" % d)
        print(json.dumps({"meta.json": meta, "body.md": body},
                         ensure_ascii=False, indent=2))
        return {"ok": True, "dry_run": True, "dir": d}
    if os.path.exists(d):
        raise SystemExit("❌ 已存在：%s" % d)
    os.makedirs(d, exist_ok=False)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    with open(os.path.join(d, "body.md"), "w", encoding="utf-8") as f:
        f.write(body)
    print("✅ 已创建素材骨架：%s" % d)
    print("   改完 meta.json / body.md 后跑：python pipeline.py build --id %s" % mid)
    return {"ok": True, "dir": d}


def cmd_scan(cfg, a):
    mats = sources.load(cfg["source"])
    st = State()
    rows = []
    for m in mats:
        errs = m.validate()
        s = st.get(m.id).get("status", "new")
        rows.append([m.id, m.title[:28], len(m.body_md), len(m.images or []),
                     s, "✅" if not errs else "❌ " + ";".join(errs)])
    table(rows, ["ID", "标题", "正文字符", "图片", "状态", "校验"])
    print("\n共 %d 条素材｜来源 %s" % (len(mats), cfg["source"].get("type")))
    return mats


def cmd_build(cfg, a):
    mats = sources.load(cfg["source"])
    if a.id:
        mats = [find(mats, a.id)]
    st = State()
    rows = []
    for m in mats:
        errs = m.validate()
        if errs and not a.force:
            rows.append([m.id, "❌ 跳过", ";".join(errs), ""])
            continue
        res = compose.render(m, cfg.get("compose", {}))
        paths = compose.write_out(res, out_dir(cfg))
        if st.get(m.id).get("status") == "new":
            st.set(m.id, "composed", title=m.title)
        mf = res["manifest"]
        rows.append([m.id, "✅ 已生成" if not mf["has_blocker"] else "⚠️ 有 ❌ 待填",
                     "%d 项核对 / 待传图 %d" % (len(mf["checks"]), len(mf["pending_upload"])),
                     paths["preview"]])
    table(rows, ["ID", "结果", "核对", "预览文件"])
    print("\n目录：%s" % out_dir(cfg))
    return rows


def cmd_probe(cfg, a):
    from cdp_vision import Cdp
    c = Cdp((cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765"))
    d = deliver.probe(c)
    m = d["main"]
    print("✅ 只读探测完成（未点击 / 未输入 / 未跳转）")
    print("URL   : %s" % m.get("url"))
    print("标题  : %s" % m.get("title"))
    print("可编辑: %d ｜ iframe: %d ｜ input: %d ｜ 文件上传框: %d ｜ 按钮: %d"
          % (len(m.get("editable", [])), len(m.get("iframes", [])),
             len(m.get("inputs", [])), len(m.get("fileInputs", [])),
             len(m.get("buttons", []))))
    print("DOM   : %s 字节 -> %s" % (d.get("dom_len", 0), d.get("dom_file", "-")))
    print("报告  : %s" % d.get("saved"))
    for e in m.get("editable", [])[:8]:
        print("   · editable <%s> id=%s cls=%s" % (e.get("tag"), e.get("id"), e.get("cls")[:50]))
    for e in m.get("iframes", [])[:8]:
        print("   · iframe id=%s src=%s" % (e.get("id"), (e.get("src") or "")[:90]))
    return d


def cmd_draft(cfg, a):
    from cdp_vision import Cdp
    m = find(sources.load(cfg["source"]), a.id)
    res = compose.render(m, cfg.get("compose", {}))
    mf = res["manifest"]
    if mf["has_blocker"] and not a.force:
        raise SystemExit("❌ 存在待填项，先用 build 核对：\n  " + "\n  ".join(mf["checks"]))
    c = Cdp((cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765"))
    mp = media_inject.plan(cfg, m.id)
    print("标题：%s ｜ 正文 %d 字符 ｜ 图片 %d ｜ 封面 %s"
          % (m.title, len(res["fragment"]), len(mp["body_images"]),
             "✅" if mp["cover"]["state"] == "ready" else "❌"))
    st = State()
    if a.dry_run:
        plan = deliver.fill(c, res["fragment"], m.title, m.author,
                            m.summary, dry_run=True)
        sel = plan["plan"]["selectors"]
        print("✅ dry-run：只做只读选择器预检，未输入 / 未点击 / 未保存\n")
        table([[k, "✅" if v.get("found") else "❌",
                "可编辑" if v.get("editable") else "表单",
                "可见" if v.get("visible") else "隐藏"]
               for k, v in sel.items() if isinstance(v, dict)],
              ["元素", "命中", "类型", "可见性"])
        p = media_inject.inject(cfg, c, m.id, dry_run=True)
        print("\n图片/封面计划：正文 %d 张、封面 key=%s ｜ 视口 %s"
              % (len(p["plan"]["body_keys"]), p["plan"]["cover_key"][:18] or "无",
                 p["plan"]["viewport"]))
        for w in p["warnings"]:
            print("⚠️ " + w)
        print("保存按钮：%s" % json.dumps(deliver.save_draft(c, dry_run=True),
                                     ensure_ascii=False))
        return plan
    if st.get(m.id).get("status") in ("drafted", "published") and not a.force:
        print("⚠️ 该素材已生成过草稿（%s）。要【修改那篇已有草稿】用 revise --id %s"
              "（不新建、不发表）；加 --force 才会另建一篇新草稿；只补图用 inject。"
              % (st.get(m.id).get("status"), m.id))
        return {"ok": True, "skipped": True}
    if not a.current:
        deliver.navigate_editor(c)
    r1 = deliver.fill(c, res["fragment"], m.title, m.author, m.summary)

    # 正文图 + 封面：走编辑器官方 UI，微信会写进 PM state（保存后可刷新复核）
    media = {"ok": True, "skipped": True}
    if not a.no_media and (mp["body_images"] or mp["cover"]["key"]):
        for w in mp["warnings"]:
            print("⚠️ " + w)
        media = media_inject.inject(cfg, c, m.id, current=True,
                                    save=False, verify_reload=False)
        print("   正文图：%s ｜ 封面：%s"
              % (media["body_images"].get("action"), media["cover"].get("action")))
    elif a.no_media:
        print("   --no-media：跳过正文图与封面")
    r2 = deliver.save_draft(c)

    # 刷新一次，确认图片/封面确实写进服务器，而不是只改了 DOM
    appmsgid, verified = "", {"body_imgs": 0, "cover": ""}
    if not a.no_reload and not a.no_media:
        c.cdp("Page.reload", {"ignoreCache": False})
        for _ in range(40):
            time.sleep(0.5)
            try:
                if deliver.ev(c, "document.readyState") == "complete" and deliver.ev(
                        c, "!!document.querySelector('#ueditor_0 .ProseMirror')"):
                    break
            except Exception:
                pass
        time.sleep(1.2)
        import re as _re
        s = deliver.ev(c, """(function(){var b=document.querySelector('#ueditor_0 .ProseMirror');
          var cv=document.querySelector('.js_cover_preview_new');var st=cv?(cv.getAttribute('style')||''):'';
          var m=st.match(/url\\((?:"|')?([^"')]+)/);return {url:location.href,
          imgs:b?Array.from(b.querySelectorAll('img')).filter(function(i){return !!i.src;}).length:0,
          cover:m?m[1]:''};})()""")
        u = str(s.get("url") or "")
        m2 = _re.search(r"appmsgid=(\d+)", u)
        appmsgid = m2.group(1) if m2 else ""
        verified = {"body_imgs": s.get("imgs", 0), "cover": s.get("cover", "")}
    st.set(m.id, "drafted", title=m.title,
           fragment_len=len(res["fragment"]), editor="#ueditor_0 .ProseMirror",
           appmsgid=appmsgid or None,
           body_imgs=verified["body_imgs"], cover_url=verified["cover"][:200] or None)
    print("✅ 已存草稿：%s" % json.dumps({
        "body_html_len": r1["plan"]["result"].get("body_html_len"),
        "title": r1["plan"]["result"].get("title_pm"),
        "body_imgs": verified["body_imgs"],
        "cover": "✅" if "mmbiz.qpic.cn" in verified["cover"] else "—",
        "appmsgid": appmsgid or "—",
        "save": r2.get("clicked")}, ensure_ascii=False))
    print("💡 正文为纯文本（可靠持久化）。要完整排版："
          "python pipeline.py copy --id %s，然后在编辑器里 Ctrl+A / Ctrl+V" % m.id)
    return r2


def cmd_inject(cfg, a):
    """给【已有草稿】补正文图片 / 封面；或以后新建草稿时由 draft 自动调用。"""
    if not a.id:
        raise SystemExit("❌ 请指定 --id（先跑 scan 看列表）")
    from cdp_vision import Cdp
    p = media_inject.plan(cfg, a.id)
    print("《%s》｜ 正文图 %d 张 ｜ 封面 %s"
          % (p["title"], len(p["body_images"]),
             "✅ " + p["cover"]["key"][:20] if p["cover"]["key"] else "❌ 无"))
    table([[str(x["index"]), x["state"], x["key"][:28] or "—", x["url"][:72]]
           for x in p["body_images"]] or [["(无)", "-", "-", "-"]],
          ["序号", "状态", "图片 ID", "微信地址"])
    for w in p["warnings"]:
        print("⚠️ " + w)
    if a.dry_run:
        r = media_inject.inject(cfg, None, a.id, dry_run=True)
        print("\n✅ dry-run：未导航 / 未点击 / 未保存")
        return r
    c = Cdp((cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765"))
    appmsgid = a.appmsgid or State().get(a.id).get("appmsgid")
    print("\n⚠️ 即将写入公众号草稿（只插图/封面 + 存草稿，不发表、不群发）")
    r = media_inject.inject(
        cfg, c, a.id, appmsgid=appmsgid, current=a.current and not appmsgid,
        do_cover=not a.no_cover, do_body=not a.no_body,
        replace_cover=a.replace_cover, reset_body=a.reset_body,
        save=not a.no_save, verify_reload=not a.no_reload)
    print("\n步骤：" + " → ".join(r["steps"]))
    v = r.get("verify", {})
    print("复核：正文图 %d 张 ｜ 封面 %s ｜ appmsgid=%s"
          % (v.get("body_imgs", 0),
             "✅" if "mmbiz.qpic.cn" in str(v.get("cover", "")) else "❌",
             r.get("appmsgid") or "—"))
    if r.get("appmsgid"):
        cur = State().get(a.id).get("status")
        State().set(a.id, cur if cur in ("new", "composed", "drafted", "published", "failed")
                    else "drafted", appmsgid=r["appmsgid"],
                    body_imgs=v.get("body_imgs", 0))
    return r


def cmd_drafts(cfg, a):
    """只读列出公众号后台草稿箱；顺手把台账里缺的 appmsgid 补登记。"""
    from cdp_vision import Cdp
    c = Cdp((cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765"))
    scan = revise.scan_drafts(c, navigate=True)
    rows = [[x["appmsgid"], x["title"][:30], "✅" if x["cover"] else "—", x["updated"]]
            for x in scan["items"]]
    table(rows or [["(空)", "-", "-", "-"]],
          ["appmsgid", "后台草稿标题", "封面", "更新于"])
    linked = revise.link_state(cfg, scan["items"])
    for x in linked:
        print("🔗 台账补登记：%s《%s》→ %s"
              % (x["id"], x["title"][:20], x["appmsgid"]))
    print("\n共 %d 篇后台草稿 ｜ 扫描时间 %s ｜ 已缓存到 drafts_cache.json"
          % (len(scan["items"]), scan["scanned_at"]))
    print("💡 改某篇：先在本地改好 → python pipeline.py revise --id <素材ID>")
    return scan


def cmd_revise(cfg, a):
    """用本地最新内容覆盖更新【已经存在】的公众号草稿（不新建）。"""
    if not a.id:
        raise SystemExit("❌ 请指定 --id（先跑 scan 看列表）")
    from cdp_vision import Cdp
    p = revise.plan(cfg, a.id, appmsgid=a.appmsgid,
                    replace_cover=a.replace_cover, no_media=a.no_media)
    print("《%s》 → 后台草稿 %s（%s）｜ 台账状态 %s"
          % (p["title"], p["appmsgid"] or "自动查找",
             p["appmsgid_source"] or "执行时按标题匹配", p["status"]))
    table([[x["label"], x["value"]] for x in p["fields"]],
          ["将覆盖的字段", "本地最新内容"])
    for w in p["warnings"]:
        print("⚠️ " + w)
    if a.dry_run:
        print("\n✅ dry-run：未导航 / 未输入 / 未点击 / 未保存")
        return p
    c = Cdp((cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765"))
    r = revise.revise(cfg, c, a.id, appmsgid=a.appmsgid,
                      replace_cover=a.replace_cover, no_media=a.no_media,
                      verify_reload=not a.no_reload, force=a.force,
                      current=a.current)
    print("\n步骤：" + " → ".join(r["steps"]))
    b, f = r["before"], r["after"]
    table([["标题", b["title"][:26], f["title"][:26]],
           ["正文长度", str(b["body_len"]), str(f["body_len"])],
           ["作者", b["author"][:20] or "—", f["author"][:20] or "—"],
           ["摘要", (b["digest"] or "—")[:20], (f["digest"] or "—")[:20]],
           ["正文图片", str(b["body_imgs"]), str(f["body_imgs"])],
           ["封面", "✅" if "mmbiz" in b["cover"] else "—",
            "✅" if "mmbiz" in f["cover"] else "—"]],
          ["字段", "改前", "改后（刷新后）"])
    print("\n✅ %s" % r["message"])
    print("💡 草稿箱里仍是同一篇（appmsgid 不变），没有多出新草稿、没有发表。")
    return r


def cmd_copy(cfg, a):
    """把带内联样式的 HTML 放进剪贴板，交给真人在编辑器里 Ctrl+V。"""
    m = find(sources.load(cfg["source"]), a.id)
    p = os.path.join(out_dir(cfg), compose.slug(m.id), "fragment.html")
    if not os.path.exists(p):
        raise SystemExit("❌ 缺少 %s，先跑 build" % p)
    frag = open(p, encoding="utf-8").read()
    r = deliver.copy_fragment_to_clipboard(frag)
    print("✅ 已把《%s》的排版 HTML 放进剪贴板" % m.title)
    print("   正文 %d 字符 ｜ CF_HTML %d 字节 ｜ 剪贴板随时可能被覆盖，请尽快粘贴\n"
          % (len(frag), r["bytes"]))
    table([
        ["1", "在公众号编辑器正文里点一下", "让光标进入正文区"],
        ["2", "Ctrl + A", "全选当前纯文本正文"],
        ["3", "Ctrl + V", "粘贴后标题/分节/引用/CTA 样式会完整保留"],
        ["4", "点「保存为草稿」", "只存草稿，不要点「发表」"],
    ], ["步骤", "动作", "说明"])
    print("\n📌 本地图片不会被自动转存，粘贴后请检查文中图片占位。")
    return r


def cmd_upload(cfg, a):
    """把素材里的本地图片上传到公众号官方素材库，并回填微信 CDN 地址。"""
    if not a.id:
        raise SystemExit("❌ 请指定 --id（先跑 scan 看列表）")
    from cdp_vision import Cdp
    c = Cdp((cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765"))
    p = upload_media.plan(cfg, a.id)
    print("《%s》｜ 本地图片 %d 张（待传 %d / 已传 %d）"
          % (p["title"], len(p["items"]), len(p["pending"]), len(p["uploaded"])))
    table([[x["name"], "%.1f MB" % (x["size"] / 1048576),
            {"pending": "待上传", "uploaded": "✅ 已上传", "remote": "🌐 网络图片",
             "error": "❌ " + x["error"]}[x["state"]],
            (x["url"] or x["ref"])[:56]]
           for x in p["items"]] or [["(无)", "-", "-", "-"]],
          ["文件", "大小", "状态", "路径 / 微信地址"])
    if a.dry_run:
        print("\n✅ dry-run：未导航 / 未上传 / 未改动任何文件")
        return {"ok": True, "dry_run": True, "plan": p}
    if not p["pending"]:
        print("\n✅ 没有需要上传的图片")
        return {"ok": True, "uploaded": []}
    print("\n⚠️ 即将写入公众号官方素材库（可在后台删除），并回填 meta.json（有 .bak 备份）")
    r = upload_media.upload(cfg, c, a.id, refs=a.file or None,
                            writeback=not a.no_writeback,
                            stay=a.stay,
                            timeout=getattr(a, "upload_timeout", 40))
    print()
    if r.get("uploaded"):
        table([[x["name"], "✅", "共 %s 条" % x["count"], x["url"][:80]]
               for x in r["uploaded"]], ["文件", "结果", "素材库", "微信 CDN 地址"])
    for x in r.get("failed", []):
        print("❌ %s：%s" % (x["name"], x["error"]))
    if r.get("writeback"):
        print("\n✅ 已回填 meta.json：%s（原文件在 meta.json.bak）" % ", ".join(r["writeback"]))
    if r.get("rebuilt"):
        rb = r["rebuilt"]
        print("✅ 已重新生成预览/排版片段｜剩余待传图：%s"
              % (len(rb.get("pending_upload", [])) if "pending_upload" in rb else rb))
    print("\n%s" % r.get("message", ""))
    return r


def cmd_publish(cfg, a):
    from cdp_vision import Cdp
    mode = (cfg.get("delivery") or {}).get("mode", "draft_only")
    m = find(sources.load(cfg["source"]), a.id)
    if mode == "draft_only":
        st = State()
        cur = st.get(m.id).get("status")
        print("🟢 当前为草稿模式（你选的 A）：流水线不会替你点『群发』。\n")
        table([
            ["1", "生成草稿", "python pipeline.py draft --id %s" % m.id],
            ["2", "打开公众号后台草稿箱", "找到《%s》并逐项核对标题/封面/图片" % m.title],
            ["3", "在编辑器里手动点『发表』", "按微信提示完成管理员确认"],
            ["4", "回来回写台账", "python pipeline.py published --id %s" % m.id],
        ], ["步骤", "动作", "命令 / 说明"])
        print("\n当前台账状态：%s ｜ 当日已发布 %d 篇"
              % (cur, st.count_published_on()))
        print("若要改成自动群发：把 config.delivery.mode 设为 auto_publish"
              "，并在命令里加 --confirm I_UNDERSTAND（群发不可撤回，不建议）")
        return {"ok": True, "mode": "draft_only", "title": m.title}

    st = State()
    mp = os.path.join(out_dir(cfg), compose.slug(m.id), "manifest.json")
    if not os.path.exists(mp):
        raise SystemExit("❌ 缺少 %s，先跑 build" % mp)
    mf = json.load(open(mp, encoding="utf-8"))
    c = Cdp((cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765"))
    r = deliver.publish(c, m.id, cfg, mf, st, a.confirm, a.force, a.dry_run)
    print("\n" + deliver.BANNER + "\n")
    table([[("✅" if g["ok"] else "❌"), g["gate"], g["note"]] for g in r["gates"]],
          ["", "发布闸门", "说明"])
    print("\n%s  %s" % ("⛔ 已拦截，未执行任何写操作" if r["blocked"] else "🟢 闸门全通过",
                     "（dry-run）" if a.dry_run else ""))
    if not r["blocked"] and not a.dry_run and r.get("ok"):
        rb = sources.report_back(cfg.get("report"), m.id, "published",
                                 title=m.title, published_at=r["record"].get("published_at", ""))
        print("发布完成 ✅ ｜ 回写素材系统：%s" % ("✅" if rb.get("ok") else "❌ " + str(rb)))
    return r


def cmd_mark(cfg, a):
    """人工在后台点完『发表』后，补记台账 + 回写素材系统。"""
    st = State()
    m = find(sources.load(cfg["source"]), a.id)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rec = st.set(m.id, "published", published_at=now,
                 title=m.title, by="manual")
    rb = sources.report_back(cfg.get("report"), m.id, "published",
                             title=m.title, published_at=now)
    print("✅ 已标记发布：%s ｜ %s" % (m.id, m.title))
    print("   发布时间 %s ｜ 回写素材系统 %s"
          % (now, "✅" if rb.get("ok") else "⚠️ 未配置或失败 " + str(rb)))
    return rec


def _print_plan(p):
    print("将删除《%s》｜ id=%s ｜ 台账状态 %s"
          % (p["title"] or p["id"], p["id"], p["status"]))
    table([[t["label"], t["kind"], str(t["files"]), "%.1f KB" % (t["bytes"] / 1024),
            t["path"]] for t in p["targets"]] or [["(无本地文件)", "-", "0", "0", "-"]],
          ["部分", "类型", "文件数", "大小", "路径"])
    print("\n合计 %d 个文件 / %.1f KB｜台账记录：%s"
          % (p["total_files"], p["total_bytes"] / 1024,
             "一并清除（回收站里留有备份）" if p["has_state"] else "无"))
    for w in p["warnings"]:
        print("⚠️ " + w)


def cmd_delete(cfg, a):
    """删除本地文章：默认移入项目内回收站，可还原；--purge 才永久删除。"""
    if not a.id:
        raise SystemExit("❌ 请指定 --id（先跑 scan 看列表）")
    p = trash.plan(cfg, a.id)
    _print_plan(p)
    if a.dry_run:
        print("\n✅ dry-run：未改动任何文件")
        return p
    if not p["targets"] and not p["has_state"]:
        raise SystemExit("❌ 没有可删除的本地内容")
    print()
    r = trash.delete(cfg, a.id, purge=a.purge, force=a.force)
    if r["purged"]:
        print("🗑️ 已永久删除：%s（不可恢复）" % r["id"])
        for x in r["removed"]:
            print("   · " + x)
    else:
        print("✅ 已移入回收站：%s" % r["token"])
        for x in r["trashed"]:
            print("   · " + x)
        print("\n还原：python pipeline.py restore --token %s" % r["token"])
        print("列表：python pipeline.py trash")
    return r


def cmd_trash(cfg, a):
    d = trash.list_trash()
    table([[x["token"], (x["title"] or x["id"])[:24], x["status"],
            str(x["files"]), x["deleted_at"]]
           for x in d["items"]] or [["(空)", "-", "-", "0", "-"]],
          ["回收站条目 token", "标题", "原状态", "文件数", "删除时间"])
    print("\n目录：%s" % d["root"])
    print("还原：python pipeline.py restore --token <token>"
          " ｜ 彻底删除：python pipeline.py purge --token <token>")
    return d


def cmd_restore(cfg, a):
    if not a.token:
        raise SystemExit("❌ 请指定 --token（先跑 trash 看列表）")
    r = trash.restore(a.token)
    print("✅ 已还原《%s》" % r["id"])
    for x in r["restored"]:
        print("   · " + x)
    return r


def cmd_purge(cfg, a):
    if not a.token and not a.all:
        raise SystemExit("❌ 请指定 --token <token> 或 --all")
    r = trash.purge(token=a.token, all_items=bool(a.all))
    print("🗑️ %s" % r["message"])
    for x in r["removed"]:
        print("   · " + x)
    return r


def cmd_ui(cfg, a):
    """启动本机素材库管理界面（只监听 127.0.0.1）。"""
    import ui_server
    ui_server.serve(getattr(a, "port", 8766))


def cmd_status(cfg, a):
    st = State()
    rows = [[k, v.get("status"), v.get("title", "")[:28], v.get("updated_at", "")]
            for k, v in st.data["items"].items()]
    table(rows or [["(空)", "-", "-", "-"]], ["ID", "状态", "标题", "更新时间"])
    print("\n当日已发布 %d 篇" % st.count_published_on())
    try:
        from cdp_vision import Cdp
        s = Cdp((cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765")).status()
        print("探针：%s ｜ cdp_ready=%s ｜ %s"
              % (s.get("state"), s.get("cdp_ready"), str(s.get("url", ""))[:100]))
    except Exception as e:
        print("探针：❌ 离线（%s）" % e)


def main():
    ap = argparse.ArgumentParser(description="素材 -> 公众号文章自动流水线")
    ap.add_argument("cmd", choices=["new", "scan", "build", "probe", "draft",
                                    "inject", "drafts", "revise", "copy",
                                    "publish", "published", "status", "ui",
                                    "delete", "trash", "restore", "purge",
                                    "upload"])
    ap.add_argument("--id", help="素材 id")
    ap.add_argument("--file", action="append",
                    help="upload：只传指定图片（meta.json 里的路径或文件名），可重复")
    ap.add_argument("--no-writeback", action="store_true",
                    help="upload：只上传，不回填 meta.json / 不重建预览")
    ap.add_argument("--stay", action="store_true",
                    help="upload：上传后留在素材库页（默认回到上传前的页面）")
    ap.add_argument("--upload-timeout", type=int, default=40,
                    help="upload：单张图片等待微信返回的秒数")
    ap.add_argument("--title", help="new 命令的素材标题")
    ap.add_argument("--config", help="配置文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写浏览器 / 不删文件")
    ap.add_argument("--current", action="store_true",
                    help="跳过导航，直接在当前编辑器页填内容（用于修补同一篇草稿）")
    ap.add_argument("--appmsgid", help="inject / revise：指定后台草稿 appmsgid")
    ap.add_argument("--no-media", action="store_true",
                    help="draft：只写纯文本正文，不插图/不设封面")
    ap.add_argument("--no-reload", action="store_true",
                    help="draft / inject：保存后不刷新复核")
    ap.add_argument("--no-cover", action="store_true", help="inject：不动封面")
    ap.add_argument("--no-body", action="store_true", help="inject：不动正文图片")
    ap.add_argument("--no-save", action="store_true", help="inject：不点保存为草稿")
    ap.add_argument("--replace-cover", action="store_true",
                    help="inject / revise：已有封面也强制替换成本地图")
    ap.add_argument("--reset-body", action="store_true",
                    help="inject：先重写正文纯文本（清掉已有插图）再插图")
    ap.add_argument("--confirm", default="", help="发布确认词 I_UNDERSTAND")
    ap.add_argument("--force", action="store_true", help="跳过校验 / 允许删已发布台账")
    ap.add_argument("--purge", action="store_true", help="delete：不进回收站，直接永久删除")
    ap.add_argument("--token", help="restore / purge：回收站条目 token")
    ap.add_argument("--all", action="store_true", help="purge：清空整个回收站")
    ap.add_argument("--port", type=int, default=8766, help="ui 命令的监听端口")
    a = ap.parse_args()
    cfg = cfg_load(a.config)
    {"new": cmd_new, "scan": cmd_scan, "build": cmd_build,
     "probe": cmd_probe, "draft": cmd_draft, "inject": cmd_inject,
     "drafts": cmd_drafts, "revise": cmd_revise,
     "copy": cmd_copy, "publish": cmd_publish, "published": cmd_mark,
     "status": cmd_status, "ui": cmd_ui,
     "delete": cmd_delete, "trash": cmd_trash,
     "restore": cmd_restore, "purge": cmd_purge,
     "upload": cmd_upload}[a.cmd](cfg, a)


if __name__ == "__main__":
    main()
