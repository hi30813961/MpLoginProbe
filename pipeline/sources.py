# -*- coding: utf-8 -*-
"""素材接入层：把任意素材系统归一成 Material 列表。

支持 4 种 source.type：
  folder  —— 本地目录（子目录含 meta.json+body.md，或平铺 .md/.json）
  json    —— 单个 JSON 文件（数组，或 {"items":[...]}）
  sqlite  —— 本地 SQLite 表 + 列名映射
  http    —— 自有素材系统 REST 接口（GET 列表 / PATCH 回写状态）

新增一种系统 = 加一个 load_xxx()，不改后面任何代码。
"""
import json
import os
import re
import sqlite3
import urllib.request

from contract import Material

UA = "MpPipeline/1.0 (local)"
HERE = os.path.dirname(os.path.abspath(__file__))


def _abs(p):
    """配置里的相对路径一律相对 pipeline/ 目录，避免换 cwd 就找不到素材。"""
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.join(HERE, p)


# ── folder ────────────────────────────────────────────────────────────────
def _read_meta(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _md_to_material(path, root):
    raw = open(path, encoding="utf-8").read()
    rel = os.path.relpath(path, root).replace("\\", "/")
    title = ""
    body = raw
    fm = {}
    if raw.startswith("---\n"):
        end = raw.find("\n---\n", 4)
        if end > 0:
            for line in raw[4:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip().strip('"').strip("'")
            body = raw[end + 5:]
    m = re.search(r"^#\s+(.+)$", body, re.M)
    if m:
        title = m.group(1).strip()
        body = body[:m.start()] + body[m.end():]
    meta_path = os.path.splitext(path)[0] + ".json"
    if os.path.exists(meta_path):
        try:
            fm.update(_read_meta(meta_path))
        except (ValueError, OSError):
            pass
    d = dict(fm)
    d.setdefault("id", rel)
    d.setdefault("title", title or os.path.splitext(os.path.basename(path))[0])
    d["body_md"] = body.strip()
    d.setdefault("source", rel)
    if d.get("images"):
        d["images"] = _resolve_images(d["images"], os.path.dirname(path))
    if d.get("cover"):
        d["cover"] = _resolve_images([d["cover"]], os.path.dirname(path))[0]
    return Material.from_dict(d)


def _resolve_images(seq, base):
    out = []
    for x in seq or []:
        if isinstance(x, dict):
            x = x.get("path") or x.get("url") or ""
        x = str(x)
        if x and not re.match(r"^(https?|data):", x) and not os.path.isabs(x):
            x = os.path.normpath(os.path.join(base, x))
        out.append(x)
    return out


def load_folder(cfg):
    root = _abs(cfg["path"])
    if not os.path.isdir(root):
        raise FileNotFoundError("素材目录不存在: %s" % root)
    recursive = cfg.get("recursive", True)
    files = []

    def walk(d):
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if os.path.isdir(p):
                if recursive:
                    walk(p)
            else:
                files.append(p)
    walk(root)

    md_files = [p for p in files if p.lower().endswith(".md")]
    md_stems = {os.path.splitext(p)[0] for p in md_files}
    metas = {}
    for p in files:
        if os.path.basename(p).lower() == "meta.json":
            try:
                metas[os.path.dirname(p)] = _read_meta(p)
            except (ValueError, OSError):
                pass

    out = []
    for p in md_files:
        m = _md_to_material(p, root)
        extra = metas.get(os.path.dirname(p), {})
        if extra:
            d = m.to_dict()
            # meta.json 是显式元数据，优先级高于从 md 正文猜出来的字段
            d.update({k: v for k, v in extra.items() if k != "body_md"})
            if d.get("cover"):
                d["cover"] = _resolve_images([d["cover"]], os.path.dirname(p))[0]
            if d.get("images"):
                d["images"] = _resolve_images(d["images"], os.path.dirname(p))
            m = Material.from_dict(d)
        out.append(m)

    for p in files:
        low = p.lower()
        if not low.endswith(".json"):
            continue
        if os.path.basename(p).lower() == "meta.json":
            continue
        if os.path.splitext(p)[0] in md_stems:
            continue                      # 同名的 .md 元数据，已合并
        try:
            obj = _read_meta(p)
        except (ValueError, OSError):
            continue
        if isinstance(obj, dict) and ("title" in obj or "body_md" in obj
                                      or "body_html" in obj):
            obj.setdefault("id", os.path.relpath(p, root).replace("\\", "/"))
            obj.setdefault("source", obj["id"])
            d = os.path.dirname(p)
            if obj.get("cover"):
                obj["cover"] = _resolve_images([obj["cover"]], d)[0]
            if obj.get("images"):
                obj["images"] = _resolve_images(obj["images"], d)
            out.append(Material.from_dict(obj))
    return out


def load_json_file(cfg):
    p = _abs(cfg["path"])
    with open(p, encoding="utf-8") as f:
        obj = json.load(f)
    rows = obj.get("items", []) if isinstance(obj, dict) else obj
    base = os.path.dirname(p)
    out = []
    for i, d in enumerate(rows):
        d = dict(d)
        d.setdefault("id", d.get("id") or "item-%03d" % (i + 1))
        if d.get("cover"):
            d["cover"] = _resolve_images([d["cover"]], base)[0]
        if d.get("images"):
            d["images"] = _resolve_images(d["images"], base)
        out.append(Material.from_dict(d))
    return out


# ── sqlite ────────────────────────────────────────────────────────────────
def load_sqlite(cfg):
    p = _abs(cfg["path"])
    table = cfg.get("table", "materials")
    mapping = cfg.get("mapping", {})
    where = cfg.get("where", "")
    sql = "SELECT * FROM %s %s" % (table, ("WHERE " + where) if where else "")
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(sql)]
    finally:
        con.close()
    out = []
    for i, row in enumerate(rows):
        d = {}
        for k, v in row.items():
            d[mapping.get(k, k)] = v
        if isinstance(d.get("tags"), str):
            d["tags"] = [t for t in re.split(r"[,，;；|]", d["tags"]) if t.strip()]
        for key in ("images", "extra"):
            if isinstance(d.get(key), str) and str(d[key]).lstrip().startswith(("[", "{")):
                try:
                    d[key] = json.loads(d[key])
                except ValueError:
                    pass
        d.setdefault("id", "row-%03d" % (i + 1))
        d.setdefault("source", "sqlite:%s" % table)
        out.append(Material.from_dict(d))
    return out


# ── http ──────────────────────────────────────────────────────────────────
def _http(req_url, cfg, method="GET", payload=None):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    headers.update(cfg.get("headers", {}))
    if cfg.get("token"):
        headers[cfg.get("token_header", "X-Api-Token")] = cfg["token"]
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(req_url, data=data, headers=headers,
                                 method=method)
    with urllib.request.urlopen(req, timeout=cfg.get("timeout", 20)) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def load_http(cfg):
    obj = _http(cfg["url"], cfg, cfg.get("method", "GET"), cfg.get("body"))
    rows = obj.get("items", obj.get("data", [])) if isinstance(obj, dict) else obj
    if isinstance(rows, dict):
        rows = rows.get("items", rows.get("list", []))
    out = []
    for i, d in enumerate(rows or []):
        d = dict(d)
        d.setdefault("id", d.get("id") or "api-%03d" % (i + 1))
        d.setdefault("source", cfg["url"])
        out.append(Material.from_dict(d))
    return out


ADAPTERS = {
    "folder": load_folder,
    "json": load_json_file,
    "sqlite": load_sqlite,
    "http": load_http,
}


def load(cfg):
    if not cfg:
        raise ValueError("config 缺少 source")
    kind = cfg.get("type", "folder")
    if kind not in ADAPTERS:
        raise ValueError("不支持的 source.type=%s（可用: %s）"
                         % (kind, ", ".join(ADAPTERS)))
    return ADAPTERS[kind](cfg)


def report_back(cfg, mid, status, **kw):
    """发布结果回写素材系统。未配置 report 段时静默跳过。"""
    if not cfg:
        return {"ok": True, "skipped": True}
    kind = cfg.get("type")
    try:
        if kind == "sqlite":
            con = sqlite3.connect(_abs(cfg["path"]))
            table = cfg.get("table", "materials")
            id_col = cfg.get("id_column", "id")
            cols = cfg.get("status_column", "publish_status")
            sets = "%s = ?" % cols
            args = [status]
            for k, v in kw.items():
                col = cfg.get("columns", {}).get(k)
                if col:
                    sets += ", %s = ?" % col
                    args.append(v)
            args.append(mid)
            con.execute("UPDATE %s SET %s WHERE %s = ?" % (table, sets, id_col), args)
            con.commit()
            con.close()
        elif kind == "http":
            _http(cfg["url"], cfg, cfg.get("method", "PATCH"),
                  dict({"id": mid, "status": status}, **kw))
        else:
            return {"ok": True, "skipped": True}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
