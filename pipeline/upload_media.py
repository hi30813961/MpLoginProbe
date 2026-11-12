# -*- coding: utf-8 -*-
"""把素材里的本地图片上传到【公众号官方素材库】，并回填微信 CDN 地址。

链路（全部走 CDP，无感化：不占鼠标、不弹系统文件框）：
  1. 导航到「素材库 → 图片」页（filepage?type=2）
  2. 读上传前的图片总数 + 第一页 mmbiz.qpic.cn URL 集合
  3. DOM.querySelector('input[type=file]') → DOM.setFileInputFiles(绝对路径)
     —— 这是 WebUploader 的隐藏 input，塞文件即触发 change 并自动上传
  4. 微信上传成功后会**整页重载**（URL 带 group_id=0&view=1），等计数 +1
     且列表出现新的 mmbiz 图片，取差量即为这张图的 CDN 地址
  5. 写回 meta.json 的 images / cover（原文件留 .bak），重新 build
     —— 之后排版 HTML 直接用微信图片地址，粘贴到编辑器就能显示

幂等：uploaded.json 记录每个「文件指纹 -> CDN 地址」，已传过的不会重复传。
安全：只上传图片、限 10 MB、只写自己的 meta.json；不删官方素材、不群发。
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime

BASE_PIPELINE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_PIPELINE)
sys.path.insert(0, os.path.dirname(BASE_PIPELINE))   # cdp_vision.py 在上一层

import compose          # noqa: E402
import sources          # noqa: E402
from cdp_vision import Cdp, CdpError  # noqa: E402
from contract import State
from deliver import ev, token_of

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_LOG = os.path.join(BASE, "uploaded.json")
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_SIZE = 10 * 1024 * 1024


def _read_log():
    if os.path.isfile(UPLOAD_LOG):
        try:
            with open(UPLOAD_LOG, encoding="utf-8") as f:
                d = json.load(f)
            d.setdefault("items", {})
            d.setdefault("by_mid", {})
            return d
        except (ValueError, OSError):
            pass
    return {"version": 1, "items": {}, "by_mid": {}}


def _write_log(d):
    tmp = UPLOAD_LOG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, UPLOAD_LOG)


def _fingerprint(path):
    st = os.stat(path)
    h = hashlib.sha1(("%s|%d|%d" % (os.path.normcase(os.path.abspath(path)),
                                    st.st_size, int(st.st_mtime))).encode("utf-8"))
    return h.hexdigest()[:20]


def mat_dir(cfg, mid):
    return os.path.join(sources._abs(cfg["source"]["path"]), str(mid))


def _resolve(ref, d):
    r = str(ref or "")
    if re.match(r"^(https?|data):", r):
        return None
    return r if os.path.isabs(r) else os.path.normpath(os.path.join(d, r))


def plan(cfg, mid):
    """只读：列出这篇文章里还需要上传的本地图片。"""
    m = None
    for x in sources.load(cfg["source"]):
        if str(x.id) == str(mid):
            m = x
            break
    if m is None:
        raise ValueError("找不到素材 %s" % mid)
    d = mat_dir(cfg, mid)
    log = _read_log()
    known = log["by_mid"].get(str(mid), {})
    # sources.load() 会把相对图片路径解析成绝对路径，直接拿它当 ref 就没法
    # 回填 meta.json。这里优先读原始 meta.json 里的字符串作为 ref。
    refs = []
    mp = os.path.join(d, "meta.json")
    if os.path.isfile(mp):
        try:
            with open(mp, encoding="utf-8") as f:
                raw = json.load(f)
            for r in (list(raw.get("images") or []) + [raw.get("cover")]):
                if r and r not in refs:
                    refs.append(str(r))
        except (ValueError, OSError):
            pass
    if not refs:
        for r in (list(m.images or []) + [m.cover]):
            if r and r not in refs:
                refs.append(str(r))
    items = []
    for i, ref in enumerate(refs):
        ap = _resolve(ref, d)
        if ap is None:                                 # 已经是网络 / 微信 CDN 地址
            is_wx = "mmbiz.qpic.cn" in str(ref) or ref in known
            nm = os.path.basename(str((known.get(ref) or {}).get("file", "")))
            if not nm:                                  # 按 URL 反查登记时的原文件名
                for rec in known.values():
                    if rec.get("url") == str(ref):
                        nm = os.path.basename(str(rec.get("file", "")))
                        break
            items.append({"ref": ref, "name": nm or ("图片 %d" % (i + 1)),
                          "abs": "", "exists": True, "size": 0, "ext": "",
                          "url": str(ref), "error": "",
                          "state": "uploaded" if is_wx else "remote"})
            continue
        ext = os.path.splitext(ap)[1].lower()
        info = {"ref": ref, "name": os.path.basename(ap), "abs": ap,
                "exists": os.path.isfile(ap),
                "size": os.path.getsize(ap) if os.path.isfile(ap) else 0,
                "ext": ext, "url": (known.get(ref) or {}).get("url", ""),
                "error": ""}
        if not info["exists"]:
            info["error"] = "文件不存在"
        elif ext not in IMG_EXT:
            info["error"] = "不支持的格式（仅 png/jpg/gif/webp/bmp）"
        elif info["size"] > MAX_SIZE:
            info["error"] = "超过 10 MB"
        if info["url"]:
            info["state"] = "uploaded"
        elif info["error"]:
            info["state"] = "error"
        else:
            info["state"] = "pending"
        items.append(info)
    return {"ok": True, "id": str(mid), "title": m.title, "dir": d,
            "items": items,
            "pending": [x for x in items if x["state"] == "pending"],
            "uploaded": [x for x in items if x["state"] in ("uploaded", "remote")]}


# ── 页面侧 ────────────────────────────────────────────────────────────────
SNAP_JS = r"""(function(){
  const q=s=>Array.from(document.querySelectorAll(s));
  const urls=[];
  q('[style*="mmbiz.qpic"]').forEach(e=>{
    const bi=getComputedStyle(e).backgroundImage||'';
    const m=bi.match(/url\((?:"|')?([^"')]+)/);
    if(m && /mmbiz\.qpic\.cn/.test(m[1])) urls.push(m[1].replace(/&amp;/g,'&'));
  });
  const n=(document.body.innerText.match(/图片（共\s*(\d+)\s*条）/)||[])[1]||'';
  return {url:location.href, ready:document.readyState, count:n, urls:urls};
})()"""


def _snap(cdp):
    return ev(cdp, SNAP_JS)


def _on_material_page(cdp):
    u = str(cdp.status().get("url", ""))
    return "filepage" in u and "type=2" in u


def goto_material_page(cdp, timeout=30):
    if _on_material_page(cdp):
        return str(cdp.status().get("url", ""))
    t = token_of(cdp)
    url = ("https://mp.weixin.qq.com/cgi-bin/filepage?type=2&begin=0&count=12"
           "&token=%s&lang=zh_CN" % t)
    r = cdp.cdp("Page.navigate", {"url": url})
    if not r.get("ok"):
        raise CdpError("导航到素材库失败: %s" % str(r)[:200])
    for _ in range(timeout * 2):
        time.sleep(0.5)
        try:
            if _on_material_page(cdp) and _snap(cdp).get("count"):
                return str(cdp.status().get("url", ""))
        except CdpError:
            pass                                  # 上下文销毁中
    raise CdpError("素材库页加载超时")


def _file_input_node(cdp):
    doc = cdp.cdp("DOM.getDocument", {"depth": -1, "pierce": True})
    root = doc.get("result", {}).get("root", {}).get("nodeId")
    if not root:
        raise CdpError("DOM.getDocument 失败")
    q = cdp.cdp("DOM.querySelector", {"nodeId": root, "selector": "input[type=file]"})
    nid = q.get("result", {}).get("nodeId")
    if not nid:
        raise CdpError("页面里找不到 input[type=file]（微信改版？）")
    return nid


def _wait_new_url(cdp, before, timeout=40):
    """等页面重载完 + 图片总数增加 + 出现新的 mmbiz 地址。"""
    old = set(before["urls"])
    try:
        n0 = int(before["count"])
    except (TypeError, ValueError):
        n0 = 0
    last = None
    for _ in range(timeout):
        time.sleep(1)
        try:
            s = _snap(cdp)
        except CdpError:
            continue                              # 页面正在重载
        if s.get("ready") != "complete" or not s.get("count"):
            continue
        try:
            n = int(s["count"])
        except (TypeError, ValueError):
            continue
        new = [u for u in s["urls"] if u not in old]
        if n > n0 and new:
            sig = (n, tuple(sorted(new)))
            if sig == last:                       # 连续两次一致 = 渲染稳定
                return {"count": n, "new_urls": new, "snapshot": s}
            last = sig
    raise CdpError("上传后 40s 内没检测到新图片（可能被风控或格式被拒）")


# ── 主流程 ────────────────────────────────────────────────────────────────
def upload(cfg, cdp, mid, refs=None, writeback=True, dry_run=False,
           stay=False, timeout=40):
    p = plan(cfg, mid)
    todo = [x for x in p["items"] if x["state"] == "pending"]
    if refs:
        wanted = set(refs)
        todo = [x for x in todo if x["ref"] in wanted or x["name"] in wanted]
    if dry_run:
        return {"ok": True, "dry_run": True, "id": mid, "title": p["title"],
                "would_upload": [{"name": x["name"], "size": x["size"]} for x in todo],
                "skipped": [{"name": x["name"], "url": x["url"]}
                            for x in p["uploaded"]],
                "message": "dry-run：未导航、未上传、未改动任何文件"}
    if not todo:
        return {"ok": True, "id": mid, "uploaded": [], "skipped": p["uploaded"],
                "message": "没有需要上传的本地图片"}
    st = cdp.status()
    if "LOGGED_IN" not in str(st.get("state", "")).upper():
        raise CdpError("探针未登录：%s" % st.get("state"))
    origin = str(st.get("url", ""))

    goto_material_page(cdp)
    before = _snap(cdp)
    results, failed = [], []
    for item in todo:
        try:
            nid = _file_input_node(cdp)          # 每次重载后 nodeId 会变
            r = cdp.cdp("DOM.setFileInputFiles", {"files": [item["abs"]], "nodeId": nid})
            if not r.get("ok"):
                raise CdpError("setFileInputFiles 失败: %s" % str(r)[:200])
            got = _wait_new_url(cdp, before, timeout=timeout)
            url = got["new_urls"][0]
            before = got["snapshot"]             # 下一张以当前状态为基线
            results.append({"ref": item["ref"], "name": item["name"],
                            "size": item["size"], "url": url,
                            "count": got["count"]})
            log = _read_log()
            rec = {"file": item["ref"], "abs": item["abs"], "url": url,
                   "mid": str(mid), "fingerprint": _fingerprint(item["abs"]),
                   "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            log["items"][rec["fingerprint"]] = rec
            log["by_mid"].setdefault(str(mid), {})[item["ref"]] = rec
            _write_log(log)
        except Exception as e:
            failed.append({"name": item["name"], "error": str(e)[:200]})

    written = []
    if writeback and results:
        written = _writeback(cfg, mid, {x["ref"]: x["url"] for x in results})
    rebuilt = None
    if written:
        try:
            for x in sources.load(cfg["source"]):
                if str(x.id) == str(mid):
                    res = compose.render(x, cfg.get("compose", {}))
                    d = (cfg.get("output") or {}).get("dir", "out")
                    out = d if os.path.isabs(d) else os.path.join(BASE, d)
                    compose.write_out(res, out)
                    rebuilt = {"checks": res["manifest"]["checks"],
                               "pending_upload": res["manifest"]["pending_upload"]}
                    break
        except Exception as e:
            rebuilt = {"error": str(e)[:200]}

    if not stay and origin and "filepage" not in origin:
        try:
            cdp.cdp("Page.navigate", {"url": origin})
        except Exception:
            pass
    return {"ok": not failed or bool(results), "id": mid, "title": p["title"],
            "uploaded": results, "failed": failed, "writeback": written,
            "rebuilt": rebuilt, "skipped": p["uploaded"],
            "message": "成功上传 %d 张%s" % (len(results),
                                        ("，失败 %d 张" % len(failed)) if failed else "")}


def _writeback(cfg, mid, mapping):
    """把 meta.json 里的本地路径换成微信 CDN 地址（原文件留 .bak）。"""
    mp = os.path.join(mat_dir(cfg, mid), "meta.json")
    with open(mp, encoding="utf-8") as f:
        meta = json.load(f)
    with open(mp + ".bak", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    changed = []
    imgs = list(meta.get("images") or [])
    for i, r in enumerate(imgs):
        if r in mapping:
            imgs[i] = mapping[r]
            changed.append("images[%d]" % i)
    meta["images"] = imgs
    if meta.get("cover") in mapping:
        meta["cover"] = mapping[meta["cover"]]
        changed.append("cover")
    tmp = mp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, mp)
    return changed
