# -*- coding: utf-8 -*-
"""本地文章的删除 / 回收站 / 还原（安全版）。

一篇「本地文章」散落在三处：
  1. 素材源  materials/<id>/        或平铺的 <id>.md / <id>.json
  2. 生成产物 out/<slug(id)>/
  3. 台账记录 state.json 的 items[id]

删除原则：
  * 默认「移入回收站」= 移到 pipeline/.trash/<时间戳>__<id>/，随时可还原；
    只有显式 purge 才会真正抹掉文件。
  * 删之前先用 plan() 列出完整清单，UI 需二次确认 + 手输 ID。
  * 所有路径经 realpath 边界校验，绝不会碰到素材库 / out / .trash 之外的东西，
    也绝不会把根目录本身当成删除目标。
  * 台账已标记 published 的文章需要 force，且明确提示「不影响公众号后台」。
"""
import json
import os
import re
import shutil
import threading
from datetime import datetime

import compose
import sources
from contract import State

BASE = os.path.dirname(os.path.abspath(__file__))
TRASH_ROOT = os.path.join(BASE, ".trash")
ID_RE = re.compile(r"^[\w][\w.\-]{0,95}$", re.UNICODE)
TOKEN_RE = re.compile(r"^\d{8}-\d{6}__[\w.\-]{1,96}$", re.UNICODE)
_LOCK = threading.Lock()


def check_id(mid):
    s = str(mid or "")
    if not ID_RE.match(s) or ".." in s or "/" in s or "\\" in s:
        raise ValueError("非法素材 id（只允许文字/数字/_/-/.，不能含路径）：%r" % s)
    return s


def _cfg_root(cfg):
    return os.path.realpath(sources._abs(cfg["source"]["path"]))


def _cfg_out(cfg):
    d = (cfg.get("output") or {}).get("dir", "out")
    return os.path.realpath(d if os.path.isabs(d) else os.path.join(BASE, d))


def _strictly_inside(path, root):
    """path 必须严格位于 root 之下（不能等于 root）。"""
    p, r = os.path.realpath(path), os.path.realpath(root)
    return p.startswith(r + os.sep) and p != r


def _stat(p):
    if os.path.isfile(p):
        return {"files": 1, "bytes": os.path.getsize(p)}
    n, b = 0, 0
    for base, _dirs, files in os.walk(p):
        for f in files:
            try:
                b += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
            n += 1
    return {"files": n, "bytes": b}


def _target(label, path, group, allowed_roots):
    if not any(_strictly_inside(path, r) for r in allowed_roots):
        raise ValueError("越界路径，拒绝操作：%s" % path)
    kind = "dir" if os.path.isdir(path) else "file"
    return {"label": label, "path": os.path.normpath(path), "kind": kind,
            "group": group, **_stat(path)}


def locate(cfg, mid):
    """只读定位：找出这篇文章在本机到底占了哪些文件。"""
    mid = check_id(mid)
    root, out = _cfg_root(cfg), _cfg_out(cfg)
    allowed = [root, out]
    targets = []

    # 1) 素材源：目录型 <root>/<id>/ 优先，其次平铺文件
    d = os.path.join(root, mid)
    if os.path.isdir(d):
        targets.append(_target("素材目录", d, "material", allowed))
    elif os.path.isfile(d):
        targets.append(_target("素材文件", d, "files", allowed))
    else:
        for ext in (".md", ".json"):
            p = d + ext
            if os.path.isfile(p):
                targets.append(_target("素材文件", p, "files", allowed))
                if ext == ".md":
                    j = d + ".json"
                    if os.path.isfile(j):
                        targets.append(_target("同名元数据", j, "files", allowed))
                break

    # 2) 生成产物 out/<slug>/
    od = os.path.join(out, compose.slug(mid))
    if os.path.isdir(od):
        targets.append(_target("生成产物", od, "out", allowed))

    return targets


def plan(cfg, mid):
    """删除前预览：返回完整清单，不改动任何文件。"""
    mid = check_id(mid)
    st = State()
    rec = st.get(mid)
    title, status = "", "new"
    try:
        for m in sources.load(cfg["source"]):
            if str(m.id) == mid:
                title, status = m.title, rec.get("status", "new")
                break
    except Exception:
        status = rec.get("status", "new")
    targets = locate(cfg, mid)
    warnings = []
    if status == "published":
        warnings.append("台账显示已发布：本地删除不影响公众号后台已发布的文章，也不会删后台草稿")
    if not targets:
        warnings.append("没有找到任何本地文件（可能来自 json/sqlite/http 素材源，需到源系统里删）")
    if rec.get("status") and mid in st.data["items"]:
        warnings.append("同时清除 state.json 里的状态记录")
    return {"ok": True, "id": mid, "title": title or rec.get("title", ""),
            "status": status, "targets": targets,
            "total_files": sum(t["files"] for t in targets),
            "total_bytes": sum(t["bytes"] for t in targets),
            "has_state": mid in st.data["items"], "warnings": warnings}


def _move_into(target, dst_root, used):
    """把 target 移到回收站条目内，返回 stored 相对路径。"""
    kind = target["group"]
    base = os.path.join(dst_root, kind)
    os.makedirs(base, exist_ok=True)
    name = os.path.basename(target["path"])
    if kind in ("material", "out"):
        # 目录型：整目录搬过去，名字固定，不会冲突（一个 token 里只出现一次）
        dst = os.path.join(base, name)
    else:
        dst = os.path.join(base, name)
        i = 1
        while os.path.normcase(dst) in used or os.path.exists(dst):
            stem, ext = os.path.splitext(name)
            dst = os.path.join(base, "%s_%d%s" % (stem, i, ext))
            i += 1
    used.add(os.path.normcase(dst))
    shutil.move(target["path"], dst)
    return os.path.relpath(dst, dst_root).replace("\\", "/")


def delete(cfg, mid, purge=False, force=False):
    """删除一篇本地文章。purge=False（默认）移入回收站，purge=True 永久删除。"""
    with _LOCK:
        p = plan(cfg, mid)
        if not p["targets"] and not p["has_state"]:
            raise ValueError("没有可删除的本地内容：%s" % p["id"])
        if p["status"] == "published" and not force:
            raise ValueError("该文章台账状态为 published，删除不影响公众号后台；"
                             "确认请加 --force（CLI）或勾选「我知道」（UI）")

        st = State()
        old_rec = st.pop(p["id"]) if p["has_state"] else None

        if purge:
            for t in p["targets"]:
                if not any(_strictly_inside(t["path"], r)
                           for r in (_cfg_root(cfg), _cfg_out(cfg))):
                    raise ValueError("越界路径，拒绝操作：%s" % t["path"])
                if os.path.isdir(t["path"]):
                    shutil.rmtree(t["path"])
                elif os.path.isfile(t["path"]):
                    os.remove(t["path"])
            return {"ok": True, "id": p["id"], "purged": True,
                    "removed": [t["path"] for t in p["targets"]],
                    "state_removed": bool(old_rec),
                    "message": "已永久删除（不可恢复）"}

        os.makedirs(TRASH_ROOT, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        token = "%s__%s" % (stamp, re.sub(r"[^\w.\-]", "-", p["id"]))
        dst = os.path.join(TRASH_ROOT, token)
        i = 1
        while os.path.exists(dst):
            dst = os.path.join(TRASH_ROOT, "%s__%s_%d" % (stamp, p["id"], i))
            token = os.path.basename(dst)
            i += 1
        os.makedirs(dst, exist_ok=False)

        used, entries = set(), []
        try:
            for t in p["targets"]:
                stored = _move_into(t, dst, used)
                entries.append({k: t[k] for k in ("label", "path", "kind", "group")}
                               | {"stored": stored})
            meta = {"token": token, "id": p["id"], "title": p["title"],
                    "status": p["status"],
                    "deleted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "entries": entries, "state_removed": bool(old_rec)}
            with open(os.path.join(dst, "restore.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            if old_rec is not None:
                with open(os.path.join(dst, "state-record.json"), "w", encoding="utf-8") as f:
                    json.dump(old_rec, f, ensure_ascii=False, indent=2)
        except Exception:
            # 中途失败：把已经搬走的东西尽量还回去，避免半吊子状态
            for e in entries:
                src = os.path.join(dst, e["stored"])
                if os.path.exists(src) and not os.path.exists(e["path"]):
                    os.makedirs(os.path.dirname(e["path"]), exist_ok=True)
                    shutil.move(src, e["path"])
            if old_rec is not None:
                st.put(p["id"], old_rec)
            shutil.rmtree(dst, ignore_errors=True)
            raise

        return {"ok": True, "id": p["id"], "token": token, "purged": False,
                "trashed": [t["path"] for t in p["targets"]],
                "state_removed": bool(old_rec),
                "message": "已移入回收站，可用 restore 还原"}


def _entry_dir(token):
    token = str(token or "")
    if not TOKEN_RE.match(token):
        raise ValueError("非法回收站条目：%r" % token)
    d = os.path.join(TRASH_ROOT, token)
    if not _strictly_inside(d, TRASH_ROOT) or not os.path.isdir(d):
        raise ValueError("回收站条目不存在：%s" % token)
    return d


def _read_entry(d):
    with open(os.path.join(d, "restore.json"), encoding="utf-8") as f:
        return json.load(f)


def list_trash():
    items = []
    if not os.path.isdir(TRASH_ROOT):
        return {"ok": True, "items": [], "root": TRASH_ROOT}
    for name in sorted(os.listdir(TRASH_ROOT), reverse=True):
        d = os.path.join(TRASH_ROOT, name)
        if not os.path.isdir(d) or not TOKEN_RE.match(name):
            continue
        try:
            meta = _read_entry(d)
        except (OSError, ValueError):
            continue
        st = _stat(d)
        items.append({"token": name, "id": meta.get("id", ""),
                      "title": meta.get("title", ""), "status": meta.get("status", ""),
                      "deleted_at": meta.get("deleted_at", ""),
                      "files": st["files"], "bytes": st["bytes"],
                      "entries": len(meta.get("entries", []))})
    return {"ok": True, "items": items, "root": TRASH_ROOT}


def restore(token):
    with _LOCK:
        d = _entry_dir(token)
        meta = _read_entry(d)
        entries = meta.get("entries", [])
        # 先整体预检：任何一个目标已存在就整体放弃，不做半套还原
        conflicts = [e["path"] for e in entries if os.path.exists(e["path"])]
        if conflicts:
            raise ValueError("目标已存在，未执行还原：\n  " + "\n  ".join(conflicts))
        allowed = None
        cfg = _load_cfg()
        allowed = (_cfg_root(cfg), _cfg_out(cfg))
        done = []
        try:
            for e in entries:
                src = os.path.join(d, e["stored"])
                if not _strictly_inside(src, d) or not os.path.exists(src):
                    raise ValueError("回收站条目缺失：%s" % e["stored"])
                if not any(_strictly_inside(e["path"], r) for r in allowed):
                    raise ValueError("越界路径，拒绝还原：%s" % e["path"])
                os.makedirs(os.path.dirname(e["path"]), exist_ok=True)
                shutil.move(src, e["path"])
                done.append(e)
            sp = os.path.join(d, "state-record.json")
            if meta.get("state_removed") and os.path.isfile(sp):
                with open(sp, encoding="utf-8") as f:
                    State().put(meta["id"], json.load(f))
        except Exception:
            for e in reversed(done):
                back = os.path.join(d, e["stored"])
                if os.path.exists(e["path"]) and not os.path.exists(back):
                    os.makedirs(os.path.dirname(back), exist_ok=True)
                    shutil.move(e["path"], back)
            raise
        shutil.rmtree(d, ignore_errors=True)
        return {"ok": True, "id": meta.get("id"), "token": token,
                "restored": [e["path"] for e in done],
                "message": "已还原到原位置"}


def purge(token=None, all_items=False):
    """彻底删除回收站条目（不可恢复）。"""
    with _LOCK:
        if all_items:
            tokens = [x["token"] for x in list_trash()["items"]]
        else:
            tokens = [token]
        removed = []
        for t in tokens:
            d = _entry_dir(t)
            shutil.rmtree(d)
            removed.append(t)
        return {"ok": True, "removed": removed, "count": len(removed),
                "message": "已彻底删除 %d 条回收站记录" % len(removed)}


def _load_cfg():
    p = os.path.join(BASE, "config.json")
    if not os.path.exists(p):
        p = os.path.join(BASE, "config.example.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)
