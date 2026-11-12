# -*- coding: utf-8 -*-
"""素材库可视化管理 UI —— 本机回环服务，零第三方依赖。

  python pipeline.py ui                 默认 http://127.0.0.1:8766
  python ui_server.py --port 8766      等价

只监听 127.0.0.1，只读写 config.source.path 指向的素材目录与 out/ 产物；
所有会写公众号后台的动作都转发给既有 pipeline.py 命令，草稿模式闸门不变。
"""
import argparse
import base64
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import urllib.request
from collections import OrderedDict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(BASE))

import compose          # noqa: E402
import deliver          # noqa: E402
import sources          # noqa: E402
import trash            # noqa: E402
from contract import Material, State  # noqa: E402

UI_DIR = os.path.join(BASE, "ui")
CFG_PATH = os.path.join(BASE, "config.json")
ID_RE = re.compile(r"^[\w][\w.\-]{0,95}$", re.UNICODE)
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
MAX_UPLOAD = 8 * 1024 * 1024

PORT = 8766
MEDIA_MAP = {}
MEDIA_LOCK = threading.Lock()

# 微信图片 CDN 有 Referer 防盗链：带 http://127.0.0.1 的 Referer 会返回
# 2KB 的「此图片来自微信公众平台 未经允许不可引用」占位图；不带 Referer
# 或带 mp.weixin.qq.com 才返回原图。预览统一走本机同源代理，绕开该限制。
REMOTE_IMG_HOSTS = {
    "mmbiz.qpic.cn", "mmbiz.qlogo.cn", "wx.qlogo.cn", "qpic.cn",
    "res.wx.qq.com", "mmbiz.wxapp.qlogo.cn", "mmbiz.qpic.cn",
}
REMOTE_IMG_MAX = 12 * 1024 * 1024
REMOTE_IMG_CACHE = OrderedDict()
REMOTE_IMG_LOCK = threading.Lock()
FORM_FIELDS = ("id", "title", "summary", "author", "cover", "images",
               "tags", "source", "created_at")


# ── 基础工具 ──────────────────────────────────────────────────────────────
def load_cfg():
    p = CFG_PATH if os.path.exists(CFG_PATH) else os.path.join(BASE, "config.example.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def root_dir(cfg):
    return sources._abs(cfg["source"]["path"])


def check_id(mid):
    s = str(mid or "")
    if not ID_RE.match(s) or ".." in s or "/" in s or "\\" in s:
        raise ValueError("非法素材 id（只允许文字/数字/_/-/.，不能含路径）：%r" % s)
    return s


def mat_dir(cfg, mid):
    return os.path.join(root_dir(cfg), check_id(mid))


def atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
        with open(path + ".bak", "w", encoding="utf-8") as f:
            f.write(old)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def out_root(cfg):
    d = (cfg.get("output") or {}).get("dir", "out")
    return d if os.path.isabs(d) else os.path.join(BASE, d)


def out_path(cfg, mid, name):
    return os.path.join(out_root(cfg), compose.slug(mid), name)


def register_media(mid, cover, images):
    """把素材里引用的本地图片登记到只读白名单，返回可预览的绝对 URL。"""
    urls = {}
    items = [("cover", cover)] + [("i%d" % i, p) for i, p in enumerate(images or [])]
    for key, p in items:
        p = str(p or "")
        if not p:
            continue
        if re.match(r"^(https?|data):", p):
            urls[key] = p
            continue
        name = os.path.basename(p.replace("\\", "/"))
        if not name:
            continue
        ap = p if os.path.isabs(p) else os.path.join(mat_dir(load_cfg(), mid), p)
        token = "%s/%s/%s" % (mid, key, name)
        with MEDIA_LOCK:
            MEDIA_MAP[token] = os.path.normpath(ap)
        urls[key] = "http://127.0.0.1:%d/media/%s" % (PORT, quote(token))
    return urls


def remote_img_ok(url):
    """严格白名单：只允许 https 的微信图片域名，防 SSRF / 内网探测。"""
    try:
        p = urlparse(str(url or ""))
        return p.scheme == "https" and (p.hostname or "").lower() in REMOTE_IMG_HOSTS
    except Exception:
        return False


def proxy_image_url(url):
    """把微信 CDN 图片换成同源绝对 URL（compose 只认 http(s)/data 开头的 src）。"""
    u = str(url or "")
    if not remote_img_ok(u):
        return u
    return "http://127.0.0.1:%d/remote-img?url=%s" % (PORT, quote(u, safe=""))


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not remote_img_ok(newurl):
            raise ValueError("重定向到非白名单域名，已拦截")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_remote_image(url):
    """服务端取图：不带 Referer（实测微信 CDN 因此返回原图），带内存 LRU 缓存。"""
    if not remote_img_ok(url):
        raise ValueError("只允许代理微信图片 CDN")
    with REMOTE_IMG_LOCK:
        hit = REMOTE_IMG_CACHE.get(url)
        if hit:
            REMOTE_IMG_CACHE.move_to_end(url)
            return hit
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    })
    # 关键：不设置 Referer 头
    opener = urllib.request.build_opener(_SafeRedirect())
    with opener.open(req, timeout=15) as r:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            raise ValueError("远端返回的不是图片：" + (ctype or "未知"))
        data = r.read(REMOTE_IMG_MAX + 1)
    if not data:
        raise ValueError("远端返回空内容")
    if len(data) > REMOTE_IMG_MAX:
        raise ValueError("图片超过 12MB，已放弃")
    item = (data, ctype)
    with REMOTE_IMG_LOCK:
        REMOTE_IMG_CACHE[url] = item
        while len(REMOTE_IMG_CACHE) > 40:
            REMOTE_IMG_CACHE.popitem(last=False)
    return item


def material_from_payload(payload):
    """表单 -> Material（不落盘，用于实时预览）。"""
    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        raise ValueError("meta 必须是对象")
    d = dict(meta)
    d.setdefault("id", payload.get("id", "preview"))
    d.setdefault("title", "未命名")
    d["body_md"] = payload.get("body_md") or ""
    m = Material.from_dict(d)
    return m


# ── API ───────────────────────────────────────────────────────────────────
def api_overview(cfg):
    st = State()
    mats = sources.load(cfg["source"])
    counts = {}
    for m in mats:
        s = st.get(m.id).get("status", "new")
        counts[s] = counts.get(s, 0) + 1
    probe = {"state": "offline"}
    try:
        from cdp_vision import Cdp
        s = Cdp((cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765")).status()
        probe = {"state": s.get("state"), "cdp_ready": s.get("cdp_ready"),
                 "url": str(s.get("url", ""))[:160]}
    except Exception as e:
        probe = {"state": "offline", "error": str(e)[:120]}
    return {"ok": True, "root": root_dir(cfg),
            "source_type": cfg["source"].get("type"),
            "mode": (cfg.get("delivery") or {}).get("mode", "draft_only"),
            "template": (cfg.get("compose") or {}).get("template", "soft"),
            "total": len(mats), "counts": counts,
            "published_today": st.count_published_on(), "probe": probe,
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def api_list(cfg):
    st = State()
    rows = []
    for m in sources.load(cfg["source"]):
        d = mat_dir(cfg, m.id)
        urls = register_media(m.id, m.cover, m.images)
        rec = st.get(m.id)
        rows.append({
            "id": m.id, "title": m.title, "summary": m.summary,
            "author": m.author, "tags": m.tags or [],
            "cover": m.cover, "cover_url": urls.get("cover", ""),
            "images": m.images or [], "body_len": len(m.body_md or ""),
            "status": rec.get("status", "new"),
            "updated_at": rec.get("updated_at", ""),
            "errs": m.validate(),
            "editable": os.path.isdir(d) and os.path.isfile(os.path.join(d, "meta.json")),
        })
    return {"ok": True, "items": rows}


def api_get(cfg, mid):
    check_id(mid)
    d = mat_dir(cfg, mid)
    meta = read_json(os.path.join(d, "meta.json"))
    body_path = os.path.join(d, "body.md")
    if meta is None or not os.path.isfile(body_path):
        raise ValueError("该素材不是目录型（缺 meta.json / body.md），暂不支持在线编辑")
    with open(body_path, encoding="utf-8") as f:
        body = f.read()
    urls = register_media(mid, meta.get("cover"), meta.get("images"))
    img_rows = []
    for i, p in enumerate(meta.get("images") or []):
        img_rows.append({"path": p, "url": urls.get("i%d" % i, ""),
                         "exists": (bool(re.match(r"^(https?|data):", str(p)))
                                    or os.path.exists(p if os.path.isabs(str(p))
                                                      else os.path.join(d, str(p))))})
    mf = read_json(out_path(cfg, mid, "manifest.json"), {})
    return {"ok": True, "id": mid, "meta": meta, "body_md": body,
            "images": img_rows, "manifest": mf,
            "has_preview": os.path.isfile(out_path(cfg, mid, "preview.html")),
            "has_fragment": os.path.isfile(out_path(cfg, mid, "fragment.html")),
            "state": State().get(mid)}


def api_save(cfg, payload):
    mid = check_id(payload.get("id"))
    d = mat_dir(cfg, mid)
    if not os.path.isdir(d):
        raise ValueError("素材目录不存在：%s" % d)
    meta_path = os.path.join(d, "meta.json")
    old = read_json(meta_path, {}) or {}
    incoming = payload.get("meta") or {}
    if not isinstance(incoming, dict):
        raise ValueError("meta 必须是对象")
    for k in FORM_FIELDS:
        if k in incoming:
            old[k] = incoming[k]
    old["id"] = mid
    old.setdefault("source", mid)
    old.setdefault("created_at", datetime.now().strftime("%Y-%m-%d"))
    for k in ("images", "tags"):
        v = old.get(k)
        if isinstance(v, str):
            old[k] = [x.strip() for x in re.split(r"[\n,，]", v) if x.strip()]
        elif not isinstance(v, list):
            old[k] = []
    old.setdefault("summary", "")
    old.setdefault("author", "")
    old.setdefault("cover", "")
    body = payload.get("body_md")
    if body is None:
        raise ValueError("缺少 body_md")
    errs = Material.from_dict(dict(old, body_md=body)).validate()
    atomic_write(meta_path, json.dumps(old, ensure_ascii=False, indent=2) + "\n")
    atomic_write(os.path.join(d, "body.md"), body)
    return {"ok": True, "id": mid, "errs": errs,
            "message": "已保存（上一版留在 .bak）"}


def api_new(cfg, payload):
    mid = check_id(payload.get("id"))
    d = mat_dir(cfg, mid)
    if os.path.exists(d):
        raise ValueError("已存在同名素材：%s" % mid)
    title = str(payload.get("title") or "未命名素材")
    meta = {"id": mid, "title": title, "summary": "", "author": "",
            "cover": "", "images": [], "tags": [], "source": mid,
            "created_at": datetime.now().strftime("%Y-%m-%d")}
    body = ("## 小标题\n\n在这里写正文。支持 **加粗**、`行内代码`、> 引用、- 列表、--- 分隔线。\n\n"
            "插图：先在右侧上传图片，再用 ![图注](0) 引用第 0 张。\n\n"
            "> 价格、授权、准确率等未确认信息一律写【待填】，不要编造。\n")
    os.makedirs(d, exist_ok=False)
    atomic_write(os.path.join(d, "meta.json"),
                 json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    atomic_write(os.path.join(d, "body.md"), body)
    return {"ok": True, "id": mid, "message": "已创建新素材"}


def api_render(cfg, payload):
    """内存渲染：把本地图片换成只读白名单 URL，供页面实时预览，不落盘。"""
    mid = check_id(payload.get("id"))
    m = material_from_payload(payload)
    m.id = mid
    urls = register_media(mid, m.cover, m.images)
    # 预览专用：微信 CDN 图片改走同源代理（绕开 Referer 防盗链）；
    # 关掉开关时保留直连，由 iframe 的 no-referrer 参数兜底。
    use_proxy = payload.get("image_proxy", True) is not False
    conv = proxy_image_url if use_proxy else (lambda u: u)
    d = m.to_dict()
    d["images"] = [conv(urls.get("i%d" % i, p)) for i, p in enumerate(m.images or [])]
    d["cover"] = conv(urls.get("cover", m.cover))
    pcfg = dict(cfg.get("compose") or {})
    pcfg["image_base_url"] = ""
    res = compose.render(Material.from_dict(d), pcfg)
    # 预览用的图片 URL 是本机代理，会掩盖「本地图片待上传」警告，
    # 所以核对清单必须用原始素材再算一遍，不能拿预览那次的结果。
    real = compose.render(m, cfg.get("compose") or {})
    return {"ok": True, "fragment": res["fragment"],
            "checks": real["manifest"]["checks"],
            "has_blocker": real["manifest"]["has_blocker"],
            "manifest": real["manifest"]}


def api_build(cfg, payload):
    mid = check_id(payload.get("id"))
    m = None
    for x in sources.load(cfg["source"]):
        if str(x.id) == mid:
            m = x
            break
    if m is None:
        raise ValueError("找不到素材 %s" % mid)
    errs = m.validate()
    res = compose.render(m, cfg.get("compose") or {})
    paths = compose.write_out(res, out_root(cfg))
    st = State()
    if st.get(mid).get("status") == "new":
        st.set(mid, "composed", title=m.title)
    return {"ok": True, "id": mid, "manifest": res["manifest"],
            "paths": paths, "message": "预览与排版片段已更新"}


def run_cli(args, timeout=150):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run([sys.executable, "pipeline.py"] + args, cwd=BASE,
                           capture_output=True, timeout=timeout, env=env)
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        return {"ok": p.returncode == 0, "code": p.returncode,
                "output": (out + err).strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "命令超时（%ss）" % timeout}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def api_draft(cfg, payload):
    mid = check_id(payload.get("id"))
    return dict(run_cli(["draft", "--id", mid]), id=mid)


def api_copy(cfg, payload):
    mid = check_id(payload.get("id"))
    p = out_path(cfg, mid, "fragment.html")
    if not os.path.isfile(p):
        raise ValueError("还没有排版片段，请先点「生成预览」")
    with open(p, encoding="utf-8") as f:
        frag = f.read()
    r = deliver.copy_fragment_to_clipboard(frag)
    return {"ok": True, "id": mid, "bytes": r.get("bytes", len(frag)),
            "message": "排版 HTML 已进剪贴板，去编辑器 Ctrl+A / Ctrl+V"}


def api_published(cfg, payload):
    mid = check_id(payload.get("id"))
    return dict(run_cli(["published", "--id", mid]), id=mid)


def api_publish_guide(cfg, payload):
    mid = check_id(payload.get("id"))
    return dict(run_cli(["publish", "--id", mid]), id=mid)


def api_upload(cfg, payload):
    mid = check_id(payload.get("id"))
    name = os.path.basename(str(payload.get("name") or "").replace("\\", "/"))
    ext = os.path.splitext(name)[1].lower()
    if not name or ext not in IMG_EXT:
        raise ValueError("只支持图片：%s" % ", ".join(sorted(IMG_EXT)))
    data = payload.get("data") or ""
    if "," in data and data.strip().startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data)
    except Exception:
        raise ValueError("图片数据无法解码")
    if not raw or len(raw) > MAX_UPLOAD:
        raise ValueError("图片为空或超过 8 MB")
    d = os.path.join(mat_dir(cfg, mid), "images")
    os.makedirs(d, exist_ok=True)
    target = os.path.join(d, name)
    with open(target, "wb") as f:
        f.write(raw)
    meta = read_json(os.path.join(mat_dir(cfg, mid), "meta.json"), {}) or {}
    imgs = list(meta.get("images") or [])
    urls = register_media(mid, meta.get("cover"), imgs + ["images/" + name])
    return {"ok": True, "path": "images/" + name, "size": len(raw),
            "url": urls.get("i%d" % len(imgs), ""),
            "message": "已存入素材目录"}


def api_upload_plan(cfg, mid):
    """只读：列出这篇文章里还待上传到公众号素材库的本地图片。"""
    import upload_media
    return upload_media.plan(cfg, mid)


def api_upload_media(cfg, payload):
    """真实上传到公众号官方素材库 + 回填微信 CDN 地址（需二次确认）。"""
    import upload_media
    mid = check_id(payload.get("id"))
    if payload.get("confirm") != "UPLOAD":
        raise ValueError("需要二次确认（confirm=UPLOAD）")
    refs = payload.get("files") or None
    if refs is not None and not isinstance(refs, list):
        raise ValueError("files 必须是数组")
    from cdp_vision import Cdp
    c = Cdp((cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765"))
    return upload_media.upload(cfg, c, mid, refs=refs,
                               writeback=bool(payload.get("writeback", True)),
                               timeout=45)


def api_inject_plan(cfg, mid):
    """只读：列出这篇文章的正文图 / 封面（用于补图对话框）。"""
    import media_inject
    return media_inject.plan(cfg, mid)


def api_inject_media(cfg, payload):
    """给已有草稿补正文图 + 封面（需二次确认，绝不发表）。"""
    import media_inject
    from cdp_vision import Cdp
    from contract import State
    mid = check_id(payload.get("id"))
    if payload.get("confirm") != "INJECT":
        raise ValueError("需要二次确认（confirm=INJECT）")
    appmsgid = payload.get("appmsgid") or State().get(mid).get("appmsgid")
    appmsgid = str(appmsgid) if appmsgid and str(appmsgid).isdigit() else None
    c = Cdp((cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765"))
    r = media_inject.inject(
        cfg, c, mid, appmsgid=appmsgid,
        current=bool(payload.get("current")) and not appmsgid,
        replace_cover=bool(payload.get("replace_cover")),
        reset_body=bool(payload.get("reset_body")),
        save=True, verify_reload=True)
    if r.get("appmsgid"):
        cur = State().get(mid).get("status")
        State().set(mid, cur if cur in ("new", "composed", "drafted", "published", "failed")
                    else "drafted", appmsgid=r["appmsgid"],
                    body_imgs=r.get("verify", {}).get("body_imgs", 0))
    return r


def api_revise_plan(cfg, mid):
    """只读：改【已存草稿】前，列出将覆盖的字段和后台草稿候选。"""
    import revise
    from cdp_vision import Cdp
    drafts = revise.read_cache()["items"]
    if not drafts:
        # 探针若正停在草稿箱列表页，就地只读一次，不主动导航
        try:
            c = Cdp((cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765"))
            if "action=list_card" in str(c.status().get("url", "")):
                drafts = revise.scan_drafts(c, navigate=False)["items"]
        except Exception:
            pass
    return revise.plan(cfg, mid, drafts=drafts)


def api_drafts_scan(cfg, payload):
    """只读扫描公众号草稿箱（会导航探针到列表页），并补登记台账 appmsgid。"""
    import revise
    from cdp_vision import Cdp
    c = Cdp((cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765"))
    scan = revise.scan_drafts(c, navigate=True)
    return dict(scan, linked=revise.link_state(cfg, scan["items"]))


def api_revise(cfg, payload):
    """改已存草稿：本地覆盖后台，需二次确认 confirm=UPDATE。"""
    import revise
    from cdp_vision import Cdp
    mid = check_id(payload.get("id"))
    if payload.get("confirm") != "UPDATE":
        raise ValueError("需要二次确认（confirm=UPDATE）")
    appmsgid = payload.get("appmsgid") or None
    if appmsgid:
        appmsgid = str(appmsgid).strip()
        if not appmsgid.isdigit():
            raise ValueError("appmsgid 必须是数字")
    c = Cdp((cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765"))
    return revise.revise(cfg, c, mid, appmsgid=appmsgid,
                         replace_cover=bool(payload.get("replace_cover")),
                         no_media=bool(payload.get("no_media")),
                         verify_reload=True,
                         force=bool(payload.get("force")), current=False)


def api_delete_plan(cfg, mid):
    """删除前只读预览：列出这篇文章占用的全部本地文件。"""
    return trash.plan(cfg, mid)


def api_delete(cfg, payload):
    """界面删除：只移入回收站，且必须手输 ID 二次确认。"""
    mid = check_id(payload.get("id"))
    if payload.get("purge"):
        raise ValueError("界面只允许移入回收站；彻底删除请到回收站里操作")
    if str(payload.get("confirm") or "") != mid:
        raise ValueError('二次确认失败：请输入素材 ID「%s」' % mid)
    return trash.delete(cfg, mid, purge=False, force=bool(payload.get("force")))


def api_trash_list(cfg):
    return trash.list_trash()


def api_restore(cfg, payload):
    return trash.restore(payload.get("token"))


def api_purge(cfg, payload):
    return trash.purge(token=payload.get("token"),
                       all_items=bool(payload.get("all")))


def api_overlay(cfg):
    """公众号后台增强面板的桥状态。"""
    import mp_bridge
    return mp_bridge.info()


def api_probe(cfg):
    try:
        from cdp_vision import Cdp
        s = Cdp((cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765")).status()
        return {"ok": True, **{k: s.get(k) for k in ("state", "cdp_ready", "url")}}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


GET_API = {"overview": api_overview, "list": api_list, "get": api_get,
           "probe": api_probe, "overlay": api_overlay,
           "delete-plan": api_delete_plan, "trash": api_trash_list,
           "upload-plan": api_upload_plan, "inject-plan": api_inject_plan,
           "revise-plan": api_revise_plan}
POST_API = {"save": api_save, "new": api_new, "render": api_render,
            "build": api_build, "draft": api_draft, "copy": api_copy,
            "published": api_published, "guide": api_publish_guide,
            "upload": api_upload, "upload-media": api_upload_media,
            "inject": api_inject_media,
            "revise": api_revise, "drafts-scan": api_drafts_scan,
            "delete": api_delete, "restore": api_restore, "purge": api_purge}


# ── HTTP ──────────────────────────────────────────────────────────────────
def send_json(h, obj, code=200):
    b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(b)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(b)


def send_bytes(h, data, ctype, code=200, cache=None):
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", cache or "no-store")
    h.end_headers()
    h.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    server_version = "MaterialUI/1.0"

    def log_message(self, *args):
        pass

    def guard(self):
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            send_json(self, {"ok": False, "error": "只允许本机访问"}, 403)
            return False
        return True

    def do_GET(self):
        if not self.guard():
            return
        u = urlparse(self.path)
        p = unquote(u.path)
        q = parse_qs(u.query)
        try:
            cfg = load_cfg()
            if p in ("/", "/index.html"):
                with open(os.path.join(UI_DIR, "index.html"), "rb") as f:
                    return send_bytes(h=self, data=f.read(),
                                      ctype="text/html; charset=utf-8")
            if p == "/favicon.ico":
                return send_bytes(self, b"", "image/x-icon", 204)
            if p.startswith("/media/"):
                token = p[len("/media/"):]
                with MEDIA_LOCK:
                    real = MEDIA_MAP.get(token)
                if not real or not os.path.isfile(real):
                    return send_json(self, {"ok": False, "error": "图片未登记或不存在"}, 404)
                if os.path.splitext(real)[1].lower() not in IMG_EXT:
                    return send_json(self, {"ok": False, "error": "非图片类型"}, 403)
                ctype = mimetypes.guess_type(real)[0] or "application/octet-stream"
                with open(real, "rb") as f:
                    return send_bytes(self, f.read(), ctype)
            if p == "/remote-img":
                try:
                    data, ctype = fetch_remote_image((q.get("url") or [""])[0])
                    return send_bytes(self, data, ctype, cache="public, max-age=3600")
                except Exception as e:
                    return send_json(self, {"ok": False, "error": "图片代理失败：%s" % e}, 502)
            if p.startswith("/preview/"):
                mid = check_id(p[len("/preview/"):].strip("/"))
                fp = out_path(cfg, mid, "preview.html")
                if not os.path.isfile(fp):
                    return send_bytes(self, "还没有生成预览，请先点「生成预览」".encode("utf-8"),
                                      "text/html; charset=utf-8", 404)
                with open(fp, "r", encoding="utf-8") as f:
                    doc = f.read()
                # 静态预览页也直连微信 CDN，同样在响应里换成本机同源地址（不改磁盘产物）
                def _pv_img(m):
                    return m.group(1) + proxy_image_url(m.group(2).replace("&amp;", "&")) + m.group(3)
                doc = re.sub(r'(<img[^>]+?src=")(https://[^"]+)(")', _pv_img, doc)
                doc = doc.replace("<head>", '<head><meta name="referrer" content="no-referrer">', 1)
                return send_bytes(self, doc.encode("utf-8"), "text/html; charset=utf-8")
            if p.startswith("/api/"):
                name = p[len("/api/"):].strip("/")
                fn = GET_API.get(name)
                if not fn:
                    return send_json(self, {"ok": False, "error": "未知接口 " + name}, 404)
                if name in ("get", "delete-plan", "upload-plan", "inject-plan",
                            "revise-plan"):
                    return send_json(self, fn(cfg, (q.get("id") or [""])[0]))
                return send_json(self, fn(cfg))
            return send_json(self, {"ok": False, "error": "Not found"}, 404)
        except Exception as e:
            return send_json(self, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 400)

    def do_POST(self):
        if not self.guard():
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 12 * 1024 * 1024:
                raise ValueError("请求体过大")
            raw = self.rfile.read(n).decode("utf-8") if n else "{}"
            payload = json.loads(raw or "{}")
            name = urlparse(self.path).path[len("/api/"):].strip("/")
            fn = POST_API.get(name)
            if not fn:
                return send_json(self, {"ok": False, "error": "未知接口 " + name}, 404)
            return send_json(self, fn(load_cfg(), payload))
        except Exception as e:
            return send_json(self, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 400)


def serve(port=8766):
    global PORT
    PORT = port
    cfg = load_cfg()
    if not os.path.isdir(root_dir(cfg)):
        raise SystemExit("❌ 素材目录不存在：%s" % root_dir(cfg))
    if not os.path.isfile(os.path.join(UI_DIR, "index.html")):
        raise SystemExit("❌ 缺少界面文件：%s" % os.path.join(UI_DIR, "index.html"))
    api_list(cfg)                      # 预热图片白名单
    try:
        import mp_bridge
        cdp_url = (cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765")
        mp_bridge.start(cdp_url, port)
        print("公众号后台增强面板： 已启用（%s）" % cdp_url)
    except Exception as e:
        print("公众号后台增强面板： 未启用（%s）" % e)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("素材库 UI 已启动： http://127.0.0.1:%d" % port)
    print("素材目录： %s" % root_dir(cfg))
    print("模式： %s（Ctrl+C 停止）" % (cfg.get("delivery") or {}).get("mode", "draft_only"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="素材库可视化管理")
    ap.add_argument("--port", type=int, default=8766)
    serve(ap.parse_args().port)
