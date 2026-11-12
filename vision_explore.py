# -*- coding: utf-8 -*-
"""OCR 视觉能力进阶集合 —— 从「点按钮」扩展到「看懂界面 / 读数据 / 验证 / 巡检」。

复用 vision_click.py 的截图、OCR、点击原语，本模块新增 7 种能力：

  map     整屏文字地图：自动分区 + 行聚类 + 脱敏，导出 JSON 与标注图
  card    段落合并：把被换行拆碎的卡片标题还原，并与接口数据交叉验证
  diff    前后帧文字差分：不依赖程序日志，判断「操作是否真的生效」
  type    OCR 定位输入框 -> 点击 -> 输入中文 -> 回车 -> diff 验证（完整读写闭环）
  patrol  白名单栏目自动巡检：逐步点击，记录导航/接口/画面变化；菜单折叠时自动展开父级
  watch   状态哨兵：轮询识别掉线、风控、成功提示等关键词
  fields  视觉字段抽取：按列对齐/同行/内嵌三种规则，把统计数字抽成 KV

用法示例：
  python vision_explore.py map
  python vision_explore.py card --check
  python vision_explore.py type --hint "输入标题/关键词" --input "阅卷" --enter
  python vision_explore.py patrol --items "首页,草稿箱,素材库,发表记录"
  python vision_explore.py diff --before a.png --after b.png
  python vision_explore.py watch --rounds 3 --interval 2
  python vision_explore.py fields --region 450,220,2050,1160

安全：patrol 只点白名单里的导航栏目；type 只做搜索框查询；均不做发布/删除/保存。
含凭据的画面文字在落盘前会脱敏（token=xxx -> token:***）。
"""
import argparse
import ctypes
import difflib
import io
import json
import os
import re
import sys
import time
from collections import Counter
from ctypes import wintypes
from datetime import datetime

from PIL import Image, ImageDraw

import vision_click as V

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "explore")
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


# ── 通用工具 ─────────────────────────────────────────────────────────────
def ensure_out():
    os.makedirs(OUT, exist_ok=True)
    return OUT


def scrub(s):
    """落盘前脱敏：画面里会出现 token / 长数字 ID。"""
    s = re.sub(r"(token\s*[:=]\s*)\d+", r"\1***", s, flags=re.I)
    s = re.sub(r"(fingerprint\s*[:=]\s*)[0-9a-f]{16,}", r"\1***", s, flags=re.I)
    return s


def dump(name, obj):
    ensure_out()
    p = os.path.join(OUT, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return p


def bbox(box):
    xs, ys = box[0::2], box[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def norm_items(res, min_score=0.45, region=None):
    """OCR 结果 -> 规整的文字块，可按图内区域裁剪。"""
    out = []
    for it in res.get("items", []):
        if it.get("score", 0) < min_score or len(it.get("box", [])) < 8:
            continue
        x1, y1, x2, y2 = bbox(it["box"])
        cx, cy = V.center_of(it["box"])
        if region and (cx < region[0] or cx > region[2]
                       or cy < region[1] or cy > region[3]):
            continue
        out.append({"text": scrub(it["text"]), "score": round(float(it["score"]), 3),
                    "x1": round(x1, 1), "y1": round(y1, 1),
                    "x2": round(x2, 1), "y2": round(y2, 1),
                    "cx": round(cx, 1), "cy": round(cy, 1),
                    "w": round(x2 - x1, 1), "h": round(y2 - y1, 1)})
    return out


def zone_of(it, W, H):
    """按相对位置给文字块打分区标签（对 1000~2500px 宽的窗口都适用）。"""
    x, y = it["cx"], it["cy"]
    if y > 0.93 * H:
        return "按钮行"
    if y > 0.78 * H:
        return "日志区"
    if y < 0.15 * H:
        return "顶部横幅"
    if x < 0.19 * W:
        return "左侧菜单"
    if x > 0.86 * W:
        return "右侧工具"
    return "主内容区"


def cluster_rows(items, tol=None):
    """按 y 中心聚类成视觉行。"""
    if not items:
        return []
    hs = sorted(i["h"] for i in items)
    med = hs[len(hs) // 2] or 20
    tol = tol if tol is not None else max(14, med * 0.6)
    rows, cur = [], []
    for it in sorted(items, key=lambda i: i["cy"]):
        if cur and it["cy"] - cur[-1]["cy"] > tol:
            rows.append(sorted(cur, key=lambda i: i["cx"]))
            cur = []
        cur.append(it)
    if cur:
        rows.append(sorted(cur, key=lambda i: i["cx"]))
    return rows


def grab(title):
    wins = V.find_window(title)
    if not wins:
        print("未找到窗口:", title)
        sys.exit(2)
    hwnd, name = wins[0]
    img, origin = V.grab_window(hwnd)
    return hwnd, name, img, origin


def shot_and_ocr(title, save=None):
    hwnd, name, img, origin = grab(title)
    if save:
        ensure_out()
        img.save(os.path.join(OUT, save))
    return hwnd, img, origin, V.ocr(img)


def annotate_zones(img, items, target_text=None):
    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out)
    palette = {"左侧菜单": "#2B6CD4", "主内容区": "#00897B", "右侧工具": "#F5A623",
               "顶部横幅": "#7E57C2", "日志区": "#9E9E9E", "按钮行": "#E53935"}
    for it in items:
        c = palette.get(it.get("zone", ""), "#888888")
        hit = target_text and target_text in it["text"]
        d.rectangle((it["x1"], it["y1"], it["x2"], it["y2"]),
                    outline="#E53935" if hit else c, width=3 if hit else 2)
        if hit:
            d.ellipse((it["cx"] - 6, it["cy"] - 6, it["cx"] + 6, it["cy"] + 6),
                      fill="#E53935")
    return out


# ── 1. 界面文字地图 ──────────────────────────────────────────────────────
def cmd_map(a):
    _, img, origin, res = shot_and_ocr(a.title, save="map_shot.png")
    W, H = img.size
    items = norm_items(res, a.score)
    for it in items:
        it["zone"] = zone_of(it, W, H)
    rows = cluster_rows(items)
    data = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "window": a.title, "image_size": [W, H], "screen_origin": list(origin),
        "ocr_ms": res.get("ms"), "blocks": len(items), "rows": len(rows),
        "zones": dict(Counter(i["zone"] for i in items)),
        "layout": [
            {"row": n, "y": round(sum(i["cy"] for i in r) / len(r), 1),
             "items": [{"text": i["text"], "zone": i["zone"],
                        "x": i["cx"], "score": i["score"]} for i in r]}
            for n, r in enumerate(rows, 1)],
        "blocks_raw": items,
    }
    p = dump("screen_map.json", data)
    annotate_zones(img, items).save(os.path.join(OUT, "map_zones.png"))
    print("文字地图: %d 块 / %d 行 / %d ms" % (len(items), len(rows), res.get("ms", 0)))
    print("分区统计:", data["zones"])
    for n, r in enumerate(rows, 1):
        txt = " | ".join("%s[%s]" % (i["text"][:26], i["zone"][:2]) for i in r)
        print("  %2d y=%5.0f  %s" % (n, data["layout"][n-1]["y"], txt[:150]))
    print("已导出:", p, " / map_zones.png")


# ── 2. 段落合并 + 接口交叉验证 ───────────────────────────────────────────
def merge_paragraphs(items, x_tol=170, gap_factor=2.6):
    """把窄卡片里被换行拆碎的文字还原成段落。"""
    cols, cur = [], []
    for it in sorted(items, key=lambda i: i["cx"]):
        if cur and it["cx"] - cur[-1]["cx"] > x_tol:
            cols.append(cur)
            cur = []
        cur.append(it)
    if cur:
        cols.append(cur)
    paras = []
    for col in cols:
        col.sort(key=lambda i: i["cy"])
        groups, g = [], []
        for it in col:
            if g:
                gap = it["cy"] - g[-1]["cy"]
                # 相对阈值适应不同字号，绝对上限防止把相邻两个控件粘成一段
                limit = min(gap_factor * max(g[-1]["h"], it["h"], 18), 100)
                if gap > limit:
                    groups.append(g)
                    g = []
            g.append(it)
        if g:
            groups.append(g)
        for g in groups:
            paras.append({
                "text": "".join(x["text"] for x in g),
                "lines": [x["text"] for x in g],
                "bbox": [min(x["x1"] for x in g), min(x["y1"] for x in g),
                         max(x["x2"] for x in g), max(x["y2"] for x in g)],
                "cx": round(sum(x["cx"] for x in g) / len(g), 1),
                "cy": round(sum(x["cy"] for x in g) / len(g), 1),
                "score": round(min(x["score"] for x in g), 3),
            })
    return sorted(paras, key=lambda p: (p["cy"], p["cx"]))


def similar(a, b):
    na = re.sub(r"[\s\-_]", "", a).lower()
    nb = re.sub(r"[\s\-_]", "", b).lower()
    return difflib.SequenceMatcher(None, na, nb).ratio()


def cmd_card(a):
    _, img, origin, res = shot_and_ocr(a.title, save="card_shot.png")
    W, H = img.size
    region = tuple(map(float, a.region.split(","))) if a.region else (
        0.19 * W, 0.15 * H, 0.86 * W, 0.78 * H)
    items = [i for i in norm_items(res, a.score, region) if len(i["text"]) >= 2]
    paras = merge_paragraphs(items)
    print("主内容区 %s 内 %d 个文字块 -> 合并成 %d 个段落"
          % (tuple(round(x) for x in region), len(items), len(paras)))
    for i, p in enumerate(paras, 1):
        print("  %2d  y=%5.0f  %d行  %s" % (i, p["cy"], len(p["lines"]), p["text"][:90]))

    cross = []
    if a.check:
        titles = []
        for f in ("parsed/drafts.json", "parsed/articles.json"):
            fp = os.path.join(BASE, f)
            if not os.path.exists(fp):
                continue
            d = json.load(open(fp, encoding="utf-8"))
            for it in d.get("items", []):
                if it.get("title"):
                    titles.append({"source": f, "title": it["title"]})
        print("\n=== 与接口数据交叉验证 ===")
        for t in titles:
            best = max(paras, key=lambda p: similar(p["text"], t["title"]))
            r = similar(best["text"], t["title"])
            flag = "✅ 对上" if r >= 0.75 else "⚠️ 偏差"
            print("  %s  %s" % (flag, t["title"]))
            print("      纯视觉还原: %s  (相似度 %.3f, 来自 %d 行)"
                  % (best["text"], r, len(best["lines"])))
            cross.append({"api_title": t["title"], "source": t["source"],
                          "ocr_paragraph": best["text"], "similarity": round(r, 3),
                          "lines": best["lines"], "match": r >= 0.75})
    p = dump("card_paragraphs.json",
             {"time": datetime.now().isoformat(timespec="seconds"),
              "region": list(region), "paragraphs": paras, "crosscheck": cross})
    print("已导出:", p)


# ── 3. 前后帧差分 ────────────────────────────────────────────────────────
def load_items(path, score):
    img = Image.open(path)
    return V.ocr(img), img


def diff_sets(before, after):
    cb, ca = Counter(i["text"] for i in before), Counter(i["text"] for i in after)
    added = list((ca - cb).elements())
    removed = list((cb - ca).elements())
    return added, removed


def cmd_diff(a):
    if a.before and a.after:
        rb, _ = load_items(a.before, a.score)
        ra, _ = load_items(a.after, a.score)
        b, af = norm_items(rb, a.score), norm_items(ra, a.score)
    else:
        print("需要提供 --before / --after 两张图片")
        sys.exit(2)
    added, removed = diff_sets(b, af)
    print("前帧 %d 块 / 后帧 %d 块" % (len(b), len(af)))
    print("新增 %d 条:" % len(added))
    for t in added[:25]:
        print("   + ", t[:80])
    print("消失 %d 条:" % len(removed))
    for t in removed[:25]:
        print("   - ", t[:80])
    changed = len(added) + len(removed)
    print("判定: %s (变化量 %d)" % ("✅ 画面确实变了" if changed else "❌ 画面无变化", changed))
    dump("diff.json", {"added": added, "removed": removed, "changed": changed})


# ── 4. 输入闭环 ──────────────────────────────────────────────────────────
def set_clipboard(text):
    """CF_UNICODETEXT 写入剪贴板（不依赖 pywin32，支持中文）。"""
    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    data = text.encode("utf-16-le") + b"\x00\x00"
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    for _ in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("剪贴板被占用")
    user32.EmptyClipboard()
    user32.SetClipboardData(CF_UNICODETEXT, h)
    user32.CloseClipboard()


def key(vk, up=False):
    user32.keybd_event(vk, 0, 0x0002 if up else 0, 0)


def chord_ctrl(char_vk):
    key(0x11)
    key(char_vk)
    time.sleep(0.05)
    key(char_vk, True)
    key(0x11, True)


def cmd_type(a):
    hwnd, img, origin, res = shot_and_ocr(a.title, save="type_before.png")
    W, H = img.size
    region = tuple(map(float, a.region.split(","))) if a.region else None
    items = norm_items(res, a.score, region)
    for i in items:
        i["zone"] = zone_of(i, W, H)
    hits = [i for i in items if a.hint in i["text"] and i["zone"] != "日志区"]
    if not hits:
        print("未找到输入框文字 %r" % a.hint)
        sys.exit(4)
    hits.sort(key=lambda i: -i["cy"])
    hit = hits[0]
    sx, sy = origin[0] + hit["cx"], origin[1] + hit["cy"]
    print("定位输入框: %r score=%.3f 屏幕(%.0f, %.0f)" % (hit["text"], hit["score"], sx, sy))
    annotate_zones(img, [{**i, "zone": zone_of(i, W, H)} for i in items],
                   a.hint).save(os.path.join(OUT, "type_target.png"))
    if a.dry_run:
        print("dry-run：未输入")
        return
    V.click(sx, sy)
    time.sleep(0.35)
    if a.clear:
        chord_ctrl(0x41)              # Ctrl+A
        time.sleep(0.15)
    if a.input:
        set_clipboard(a.input)
        chord_ctrl(0x56)              # Ctrl+V
    else:
        key(0x2E)                     # Delete：输入为空表示清空
        key(0x2E, True)
    time.sleep(0.4)
    if a.enter:
        key(0x0D)
        key(0x0D, True)
    time.sleep(a.delay)
    hwnd, img2, origin2, res2 = shot_and_ocr(a.title, save="type_after.png")
    b, af = items, norm_items(res2, a.score)
    added, removed = diff_sets(b, af)
    print("%s %r%s" % ("已输入" if a.input else "已清空", a.input,
                       " 并回车" if a.enter else ""))
    print("画面变化: +%d / -%d 条文字" % (len(added), len(removed)))
    for t in added[:12]:
        print("   + ", t[:80])
    for t in removed[:8]:
        print("   - ", t[:80])
    dump("type_result.json",
         {"time": datetime.now().isoformat(timespec="seconds"),
          "hint": a.hint, "input": a.input, "enter": a.enter,
          "target": hit, "added": added, "removed": removed})


# ── 5. 栏目巡检 ──────────────────────────────────────────────────────────
def counters():
    nav = os.path.join(BASE, "nav_history.txt")
    allj = os.path.join(BASE, "responses", "all.jsonl")
    n = sum(1 for _ in open(nav, encoding="utf-8")) if os.path.exists(nav) else 0
    m = sum(1 for _ in open(allj, encoding="utf-8")) if os.path.exists(allj) else 0
    return n, m


def cmd_patrol(a):
    names = [s.strip() for s in a.items.split(",") if s.strip()]
    hwnd, name, img0, origin0 = grab(a.title)
    W, H = img0.size
    # 只在左侧菜单带内找，且排除日志/按钮行，避免误点
    region = (0, 0.15 * H, 0.19 * W, 0.78 * H)
    report = []
    print("巡检窗口: %s  %dx%d  左侧菜单区=%s" %
          (name, W, H, tuple(round(x) for x in region)))
    for step, kw in enumerate(names, 1):
        n0, m0 = counters()
        hwnd, img, origin, res = shot_and_ocr(a.title)
        expanded = False
        items = [i for i in norm_items(res, a.score, region) if kw in i["text"]]
        # 菜单折叠时子项会消失：自动点父级展开，重新截图再找一次
        if not items and a.expand and not a.dry_run:
            parent = [i for i in norm_items(res, a.score, region)
                      if a.expand in i["text"]]
            if parent:
                px = origin[0] + parent[0]["cx"]
                py = origin[1] + parent[0]["cy"]
                V.click(px, py)
                time.sleep(0.9)
                hwnd, img, origin, res = shot_and_ocr(a.title)
                expanded = True
                items = [i for i in norm_items(res, a.score, region)
                         if kw in i["text"]]
        if not items:
            print("  %d. %-10s ❌ 菜单里未找到（已尝试展开 %s）"
                  % (step, kw, a.expand or "无"))
            report.append({"step": step, "name": kw, "found": False,
                           "tried_expand": expanded})
            continue
        hit = items[0]
        sx, sy = origin[0] + hit["cx"], origin[1] + hit["cy"]
        before = norm_items(res, a.score)
        if not a.dry_run:
            V.click(sx, sy)
            time.sleep(a.delay)
        _, img2, _, res2 = shot_and_ocr(a.title)
        after = norm_items(res2, a.score)
        added, removed = diff_sets(before, after)
        n1, m1 = counters()
        print("  %d. %-10s ✅ 屏幕(%4.0f,%4.0f) 画面 +%-3d/-%-3d  导航 +%d  响应 +%d  %s"
              % (step, kw, sx, sy, len(added), len(removed), n1 - n0, m1 - m0,
                 "[自动展开]" if expanded else ""))
        report.append({"step": step, "name": kw, "found": True,
                       "auto_expanded": expanded,
                       "screen": [round(sx), round(sy)],
                       "text_added": added[:20], "text_removed": removed[:20],
                       "nav_delta": n1 - n0, "api_delta": m1 - m0,
                       "new_text_sample": [i["text"] for i in after[:12]]})
    p = dump("patrol.json",
             {"time": datetime.now().isoformat(timespec="seconds"),
              "items": names, "dry_run": a.dry_run, "steps": report})
    ok = sum(1 for r in report if r.get("found"))
    print("巡检结束: %d/%d 个栏目命中" % (ok, len(names)), " 报告:", p)


# ── 7. 视觉字段抽取（接口里没有的统计/计数数据） ────────────────────────
NUM_RE = re.compile(r"^[\d,]+(?:\.\d+)?(?:万|亿|%|w|k)?$")


def is_num(t):
    return bool(NUM_RE.match(t.strip()))


def cmd_fields(a):
    _, img, origin, res = shot_and_ocr(a.title, save="fields_shot.png")
    W, H = img.size
    region = tuple(map(float, a.region.split(","))) if a.region else (
        0.19 * W, 0.15 * H, 0.86 * W, 0.78 * H)
    items = norm_items(res, a.score, region)
    rows = cluster_rows(items)
    column_kv, inline_kv, embedded, meta = [], [], [], {}

    # ① 相邻两行、x 同列：上行标签 + 下行数字（统计卡片）
    consumed = set()
    for r1, r2 in zip(rows, rows[1:]):
        for low in r2:
            if not is_num(low["text"]):
                continue
            cands = [u for u in r1 if abs(u["cx"] - low["cx"]) < 80
                     and not is_num(u["text"])]
            if cands:
                up = min(cands, key=lambda u: abs(u["cx"] - low["cx"]))
                column_kv.append({"label": up["text"], "value": low["text"],
                                  "x": round(low["cx"]), "y": round(low["cy"]),
                                  "row_gap": round(low["cy"] - up["cy"])})
                consumed.add(id(low))
    # ② 同一行内左右相邻：标签 + 数字
    for r in rows:
        for j, it in enumerate(r):
            if is_num(it["text"]) and j > 0 and id(it) not in consumed:
                prev = r[j - 1]
                if not is_num(prev["text"]) and 0 < it["cx"] - prev["cx"] < 280:
                    inline_kv.append({"label": prev["text"], "value": it["text"],
                                      "x": round(it["cx"]), "y": round(it["cy"])})
    # ③ 同一文字块里内嵌的数值 / 统计时间 / 总数
    for it in items:
        t = it["text"]
        for m in re.finditer(r"([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z()（）]{1,12})"
                             r"\s*[:：]?\s*([\d,]+(?:\.\d+)?(?:万|亿|%)?)", t):
            embedded.append({"label": m.group(1), "value": m.group(2), "text": t[:60]})
        m = re.search(r"数据统计时间\s*[:：]\s*(.+)", t)
        if m:
            meta["数据统计时间"] = m.group(1).strip()
        m = re.search(r"共\s*(\d+)\s*条", t)
        if m:
            meta["总数"] = m.group(1)

    print("视觉字段抽取：%d 个文字块 / %d 行" % (len(items), len(rows)))
    print("  列对齐 KV (%d):" % len(column_kv))
    for kv in column_kv:
        print("     %-20s = %-8s  (x=%s, 行距=%s)" %
              (kv["label"], kv["value"], kv["x"], kv["row_gap"]))
    print("  同行 KV (%d):" % len(inline_kv))
    for kv in inline_kv:
        print("     %-20s = %s" % (kv["label"], kv["value"]))
    print("  内嵌数值 (%d):" % len(embedded))
    for kv in embedded[:10]:
        print("     %-16s = %-6s  <- %s" % (kv["label"], kv["value"], kv["text"][:40]))
    print("  元信息:", meta or "无")
    dump("visual_fields.json",
         {"time": datetime.now().isoformat(timespec="seconds"),
          "region": list(region), "column_kv": column_kv,
          "inline_kv": inline_kv, "embedded": embedded, "meta": meta,
          "all_text": [i["text"] for i in items]})


# ── 6. 状态哨兵 ──────────────────────────────────────────────────────────
ALERTS = {
    "登录失效": ["扫码", "二维码", "重新登录", "登录已过期", "会话已过期", "账号已退出"],
    "风控": ["操作频繁", "验证码", "异常", "限制", "请稍后再试"],
    "成功提示": ["保存成功", "发表成功", "已保存", "发布成功", "操作成功"],
    "错误": ["失败", "错误", "系统繁忙"],
}


def cmd_watch(a):
    log = []
    for r in range(1, a.rounds + 1):
        _, img, origin, res = shot_and_ocr(a.title, save="watch_last.png")
        items = norm_items(res, a.score)
        texts = [i["text"] for i in items]
        hitmap = {}
        for kind, kws in ALERTS.items():
            for kw in kws:
                got = [t for t in texts if kw in t]
                if got:
                    hitmap.setdefault(kind, []).extend(got)
        state = open(os.path.join(BASE, "status.txt"), encoding="utf-8").read().splitlines()[0] \
            if os.path.exists(os.path.join(BASE, "status.txt")) else "?"
        line = "[%s] 第%d轮  文字块=%d  状态=%s  告警=%s" % (
            datetime.now().strftime("%H:%M:%S"), r, len(items), state,
            {k: v[:3] for k, v in hitmap.items()} or "无")
        print(line)
        log.append({"round": r, "time": datetime.now().isoformat(timespec="seconds"),
                    "blocks": len(items), "state": state, "alerts": hitmap,
                    "texts": texts})
        if r < a.rounds:
            time.sleep(a.interval)
    dump("watch.json", log)
    print("哨兵记录已导出 explore/watch.json")


# ── CLI ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="OCR 视觉能力进阶集合")
    ap.add_argument("cmd", choices=["map", "card", "diff", "type",
                                    "patrol", "watch", "fields"])
    ap.add_argument("--title", default="公众号后台会话探针")
    ap.add_argument("--score", type=float, default=0.45)
    ap.add_argument("--region", help="图内区域 x1,y1,x2,y2")
    ap.add_argument("--check", action="store_true", help="card: 与接口数据交叉验证")
    ap.add_argument("--before"), ap.add_argument("--after")
    ap.add_argument("--hint", help="type: 输入框占位文字")
    ap.add_argument("--input", help="type: 要输入的文字")
    ap.add_argument("--enter", action="store_true")
    ap.add_argument("--clear", action="store_true", help="type: 输入前 Ctrl+A")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--items", default="首页,草稿箱,素材库,发表记录")
    ap.add_argument("--expand", default="内容管理",
                    help="patrol: 子菜单找不到时先点这个父级展开，留空则关闭")
    ap.add_argument("--delay", type=float, default=1.6)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--interval", type=float, default=2)
    a = ap.parse_args()
    {"map": cmd_map, "card": cmd_card, "diff": cmd_diff, "type": cmd_type,
     "patrol": cmd_patrol, "watch": cmd_watch, "fields": cmd_fields}[a.cmd](a)


if __name__ == "__main__":
    main()
