# -*- coding: utf-8 -*-
"""CDP 无感化视觉操作后端 —— 不截屏、不占鼠标、不需要窗口在前台。

与 vision_click.py / vision_explore.py 的区别：
  screen 后端 : ImageGrab 抓屏幕 DC + 真实鼠标点击，要求窗口置前、不能被遮挡
  CDP   后端 : Page.captureScreenshot + Input.dispatchMouseEvent，窗口被别的程序
              盖住、甚至不在前台也能照常操作，物理鼠标完全不动

前置：MpLoginProbe.exe v3 正在运行（本地回环 127.0.0.1:8765）。

用法：
  python cdp_vision.py test                      # 无感化验证套件（最小化 + 免鼠标 + 点击闭环）
  python cdp_vision.py map                       # CDP 截图的界面文字地图
  python cdp_vision.py click --text "草稿箱"      # OCR 定位 + CDP 点击
  python cdp_vision.py type --hint "输入标题/关键词" --input "阅卷" --enter
  python cdp_vision.py patrol --items "草稿箱,素材库,首页"
  python cdp_vision.py fields                    # 视觉字段抽取
  python cdp_vision.py diff --before a.png --after b.png

安全：patrol 只点白名单导航栏目；type 只做搜索框查询；不做发布/删除/保存。
"""
import argparse
import ctypes
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from datetime import datetime

from PIL import Image, ImageDraw

import vision_click as V
import vision_explore as E

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "explore")
CDP_URL = os.environ.get("MP_CDP_URL", "http://127.0.0.1:8765")
user32 = ctypes.windll.user32


class CdpError(Exception):
    pass


class Cdp:
    """探针暴露的本地 CDP HTTP 通道。"""

    def __init__(self, base=CDP_URL):
        self.base = base.rstrip("/")
        self._metrics = None
        self._sx = self._sy = 1.0      # PNG 像素 -> CSS 像素 的换算系数

    def _req(self, path, data=None, timeout=45):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        r = urllib.request.Request(
            self.base + path, data=body,
            headers={"Content-Type": "application/json"},
            method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise CdpError("%s -> HTTP %d: %s" %
                           (path, e.code, e.read().decode("utf-8", "replace")[:300]))
        except Exception as e:
            raise CdpError("%s -> %s: %s" % (path, type(e).__name__, e))

    def status(self):
        return json.loads(self._req("/status"))

    def metrics(self, refresh=False):
        if self._metrics is None or refresh:
            m = json.loads(self._req("/metrics"))
            if not m.get("ok"):
                raise CdpError("metrics 失败: %s" % m)
            self._metrics = m
        return self._metrics

    def screenshot(self, save=None):
        raw = self._req("/screenshot", timeout=60)
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        m = self.metrics()
        # 高 DPI 下 PNG 是物理像素、CDP Input.* 要 CSS 像素，必须换算
        self._sx = im.width / m["css_width"]
        self._sy = im.height / m["css_height"]
        if save:
            os.makedirs(OUT, exist_ok=True)
            im.save(os.path.join(OUT, save))
        return im

    def items(self, score=0.45, region=None, save=None):
        im = self.screenshot(save=save)
        res = V.ocr(im)
        if not res.get("ok"):
            raise CdpError("OCR 失败: %s" % str(res)[:200])
        return im, E.norm_items(res, score, region)

    def click_px(self, x_png, y_png):
        """x/y 是 PNG 图内像素坐标，内部换算成 CSS 像素后交给 CDP。"""
        return json.loads(self._req("/click",
                                    {"x": x_png / self._sx, "y": y_png / self._sy}))

    def text(self, t):
        return json.loads(self._req("/text", {"text": t}))

    def key(self, name):
        return json.loads(self._req("/key", {"key": name}))

    def cdp(self, method, params=None):
        return json.loads(self._req("/cdp", {"method": method, "params": params or {}}))


# ── 通用工具 ─────────────────────────────────────────────────────────────
def dump(name, obj):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return p


def nav_count():
    p = os.path.join(BASE, "nav_history.txt")
    return sum(1 for _ in open(p, encoding="utf-8")) if os.path.exists(p) else 0


def find_hits(items, text, pick="first"):
    hits = [i for i in items if text in i["text"]]
    if pick == "bottom":
        hits.sort(key=lambda i: -i["cy"])
    elif pick == "top":
        hits.sort(key=lambda i: i["cy"])
    elif pick == "score":
        hits.sort(key=lambda i: -i["score"])
    return hits


def annotate(im, items, target=None):
    out = im.copy()
    d = ImageDraw.Draw(out)
    for i in items:
        hit = bool(target and target in i["text"])
        d.rectangle((i["x1"], i["y1"], i["x2"], i["y2"]),
                    outline="#E53935" if hit else "#2B6CD4", width=3 if hit else 1)
        if hit:
            d.ellipse((i["cx"] - 6, i["cy"] - 6, i["cx"] + 6, i["cy"] + 6), fill="#E53935")
    return out


def cursor_pos():
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def minimize_probe():
    """把探针最小化 —— 窗口完全不在屏幕上，这是最彻底的无感化验证。"""
    wins = V.find_window("公众号后台会话探针")
    if not wins:
        return None
    hwnd = wins[0][0]
    user32.ShowWindow(hwnd, 6)      # SW_MINIMIZE
    time.sleep(0.8)
    return hwnd


def restore_probe(hwnd):
    if hwnd:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.3)


# ── 1. 无感化验证套件 ────────────────────────────────────────────────────
def cmd_test(a):
    c = Cdp()
    st = c.status()
    print("=== CDP 无感化验证 ===")
    print("登录态: %s   cdp_ready=%s   url=%s" %
          (st.get("state"), st.get("cdp_ready"), st.get("url", "")[:80]))
    if not st.get("cdp_ready"):
        print("❌ WebView2 未就绪")
        sys.exit(2)

    mouse0 = cursor_pos()
    mouse1 = mouse0
    hwnd = minimize_probe()
    iconic = bool(user32.IsIconic(hwnd)) if hwnd else False
    foreground = user32.GetForegroundWindow()
    print("探针已最小化: %s   前台窗口不是探针: %s" % (iconic, foreground != hwnd))

    checks = [
        ("探针窗口已最小化（屏幕上不可见）", iconic, "IsIconic=%s" % iconic),
        ("前台窗口不是探针", foreground != hwnd,
         "foreground=%s probe=%s" % (foreground, hwnd)),
    ]
    try:
        im, items = c.items(a.score, save="cdp_test_shot.png")
        texts = " ".join(i["text"] for i in items)
        has_page = any(k in texts for k in ("公众号", "内容管理", "草稿箱", "首页", "素材库"))
        checks.append(("最小化下仍截到完整网页内容", has_page,
                       "%d 个文字块，PNG=%s" % (len(items), im.size)))

        n0 = nav_count()
        hits = find_hits(items, a.text, a.pick)
        if not hits:
            print("⚠️ 未找到 %r，可用文字：" % a.text)
            print("   ", [i["text"][:24] for i in items[:20]])
            checks.append(("定位目标文字 %r" % a.text, False, "未找到"))
        else:
            hit = hits[0]
            print("定位 %r: score=%.3f  PNG(%.0f,%.0f) -> CSS(%.1f,%.1f)" %
                  (hit["text"], hit["score"], hit["cx"], hit["cy"],
                   hit["cx"] / c._sx, hit["cy"] / c._sy))
            annotate(im, items, a.text).save(os.path.join(OUT, "cdp_test_target.png"))
            r = c.click_px(hit["cx"], hit["cy"])
            print("CDP /click ->", r)
            checks.append(("CDP 点击命令成功", bool(r.get("ok")), str(r)))
            time.sleep(a.delay)
            n1 = nav_count()
            im2, items2 = c.items(a.score, save="cdp_test_after.png")
            added, removed = E.diff_sets(items, items2)
            # 点「内容管理」这类菜单只展开、不改 URL，所以用画面变化而非导航增量判定
            checks.append(("点击后画面确实变化", len(added) + len(removed) > 0,
                           "+%d / -%d 条文字，导航 %d->%d"
                           % (len(added), len(removed), n0, n1)))
            st2 = c.status()
            checks.append(("登录态保持", st2.get("state") == "LOGGED_IN", st2.get("state")))

        mouse1 = cursor_pos()
        checks.append(("物理鼠标位置未变", mouse0 == mouse1,
                       "%s -> %s" % (mouse0, mouse1)))
    finally:
        restore_probe(hwnd)

    print("\n--- 结果 ---")
    for name, ok, detail in checks:
        print("  %s %-32s %s" % ("✅" if ok else "❌", name, detail))
    passed = sum(1 for _, ok, _ in checks if ok)
    print("通过 %d/%d" % (passed, len(checks)))
    dump("cdp_test.json",
         {"time": datetime.now().isoformat(timespec="seconds"),
          "mouse_before": list(mouse0), "mouse_after": list(mouse1),
          "minimized": iconic,
          "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks],
          "passed": passed, "total": len(checks)})


# ── 2. 文字地图 ──────────────────────────────────────────────────────────
def cmd_map(a):
    c = Cdp()
    im, items = c.items(a.score, save="cdp_map_shot.png")
    rows = E.cluster_rows(items)
    m = c.metrics()
    print("CDP 截图 %s  CSS 视口 %s×%s  缩放 %.3f×%.3f" %
          (im.size, m["css_width"], m["css_height"], c._sx, c._sy))
    print("文字块 %d / 视觉行 %d" % (len(items), len(rows)))
    for n, r in enumerate(rows, 1):
        y = sum(i["cy"] for i in r) / len(r)
        print("  %2d y=%5.0f  %s" % (n, y, " | ".join(i["text"][:30] for i in r)[:150]))
    annotate(im, items).save(os.path.join(OUT, "cdp_map_zones.png"))
    dump("cdp_map.json",
         {"time": datetime.now().isoformat(timespec="seconds"),
          "image_size": list(im.size), "metrics": m, "blocks": items,
          "layout": [{"row": n, "y": round(sum(i["cy"] for i in r) / len(r), 1),
                      "items": [i["text"] for i in r]} for n, r in enumerate(rows, 1)]})


# ── 3. 定位 + 点击 ───────────────────────────────────────────────────────
def cmd_click(a):
    c = Cdp()
    im = c.screenshot(save="cdp_click_before.png")
    items = E.norm_items(V.ocr(im), a.score)
    region = parse_region(a.region, im.size)
    hits = find_hits(filter_region(items, region), a.text, a.pick)
    if not hits:
        print("❌ 未找到 %r" % a.text)
        sys.exit(4)
    hit = hits[0]
    annotate(im, items, a.text).save(os.path.join(OUT, "cdp_click_target.png"))
    print("命中 %r score=%.3f PNG(%.0f,%.0f) -> CSS(%.1f,%.1f)" %
          (hit["text"], hit["score"], hit["cx"], hit["cy"],
           hit["cx"] / c._sx, hit["cy"] / c._sy))
    if a.dry_run:
        print("dry-run：未点击")
        return
    n0 = nav_count()
    print("CDP /click ->", c.click_px(hit["cx"], hit["cy"]))
    time.sleep(a.delay)
    im2, items2 = c.items(a.score, save="cdp_click_after.png")
    added, removed = E.diff_sets(items, items2)
    n1 = nav_count()
    print("画面 +%d/-%d 条文字   导航 %d -> %d   %s" %
          (len(added), len(removed), n0, n1,
           "✅ 页面已切换" if n1 > n0 or added else "⚠️ 无明显变化"))
    dump("cdp_click.json", {"time": datetime.now().isoformat(timespec="seconds"),
                            "text": a.text, "target": hit, "nav_delta": n1 - n0,
                            "added": added[:30], "removed": removed[:30]})


def parse_region(spec, im_size):
    if not spec:
        return None
    parts = [float(x) for x in spec.split(",")]
    if im_size and all(0 <= v <= 1 for v in parts):
        W, H = im_size
        return (parts[0] * W, parts[1] * H, parts[2] * W, parts[3] * H)
    return tuple(parts)


def filter_region(items, region):
    if not region:
        return items
    x1, y1, x2, y2 = region
    return [i for i in items if x1 <= i["cx"] <= x2 and y1 <= i["cy"] <= y2]


# ── 4. 中文输入闭环 ──────────────────────────────────────────────────────
def cmd_type(a):
    c = Cdp()
    im, items = c.items(a.score, save="cdp_type_before.png")
    region = parse_region(a.region, im.size)
    hits = find_hits(filter_region(items, region), a.hint, "bottom")
    if not hits:
        print("❌ 未找到输入框 %r" % a.hint)
        sys.exit(4)
    hit = hits[0]
    annotate(im, items, a.hint).save(os.path.join(OUT, "cdp_type_target.png"))
    print("定位 %r score=%.3f  CSS(%.1f,%.1f)" %
          (hit["text"], hit["score"], hit["cx"] / c._sx, hit["cy"] / c._sy))
    if a.dry_run:
        print("dry-run：未输入")
        return
    print("点击 ->", c.click_px(hit["cx"], hit["cy"]))
    time.sleep(0.3)
    if a.clear:
        print("Ctrl+A ->", c.key("Ctrl+A"))
        time.sleep(0.15)
    if a.input:
        print("insertText ->", c.text(a.input))
    else:
        print("Delete ->", c.key("Delete"))
    if a.enter:
        time.sleep(0.2)
        print("Enter ->", c.key("Enter"))
    time.sleep(a.delay)
    im2, items2 = c.items(a.score, save="cdp_type_after.png")
    added, removed = E.diff_sets(items, items2)
    st = c.status()
    print("画面 +%d/-%d 条文字" % (len(added), len(removed)))
    for t in added[:10]:
        print("   + ", t[:80])
    print("当前 URL:", st.get("url", "")[:160])
    dump("cdp_type.json", {"time": datetime.now().isoformat(timespec="seconds"),
                           "hint": a.hint, "input": a.input, "enter": a.enter,
                           "url": st.get("url"), "added": added[:30],
                           "removed": removed[:30]})


# ── 5. 栏目巡检 ──────────────────────────────────────────────────────────
def cmd_patrol(a):
    c = Cdp()
    names = [s.strip() for s in a.items.split(",") if s.strip()]
    report = []
    print("CDP 巡检（无需窗口前台）")
    for step, kw in enumerate(names, 1):
        im, items = c.items(a.score)
        W, H = im.size
        region = (0, 0.10 * H, 0.22 * W, 0.92 * H)
        left = filter_region(items, region)
        hits = find_hits(left, kw, "first")
        expanded = False
        # 菜单折叠时子项消失：点父级展开，重新截图再找
        if not hits and a.expand and not a.dry_run:
            parent = find_hits(left, a.expand, "first")
            if parent:
                c.click_px(parent[0]["cx"], parent[0]["cy"])
                time.sleep(0.8)
                im, items = c.items(a.score)
                left = filter_region(items, region)
                hits = find_hits(left, kw, "first")
                expanded = True
        if not hits:
            print("  %d. %-10s ❌ 未找到" % (step, kw))
            report.append({"step": step, "name": kw, "found": False,
                           "tried_expand": expanded})
            continue
        hit = hits[0]
        n0 = nav_count()
        before = items
        if not a.dry_run:
            c.click_px(hit["cx"], hit["cy"])
            time.sleep(a.delay)
        im2, after = c.items(a.score)
        added, removed = E.diff_sets(before, after)
        n1 = nav_count()
        print("  %d. %-10s ✅ CSS(%4.0f,%4.0f)  画面 +%-3d/-%-3d  导航 +%d %s"
              % (step, kw, hit["cx"] / c._sx, hit["cy"] / c._sy,
                 len(added), len(removed), n1 - n0,
                 "[自动展开]" if expanded else ""))
        report.append({"step": step, "name": kw, "found": True,
                       "auto_expanded": expanded, "css": [hit["cx"] / c._sx, hit["cy"] / c._sy],
                       "nav_delta": n1 - n0, "added": added[:20], "removed": removed[:20]})
    dump("cdp_patrol.json", {"time": datetime.now().isoformat(timespec="seconds"),
                             "backend": "cdp", "steps": report})
    print("命中 %d/%d" % (sum(1 for r in report if r.get("found")), len(names)))


# ── 6. 视觉字段抽取（规则与 vision_explore 一致） ────────────────────────
def cmd_fields(a):
    c = Cdp()
    im, items = c.items(a.score, save="cdp_fields_shot.png")
    W, H = im.size
    region = parse_region(a.region, im.size) or (0.18 * W, 0.10 * H, 0.92 * W, 0.90 * H)
    items = filter_region(items, region)
    rows = E.cluster_rows(items)
    column_kv, inline_kv, embedded, meta = [], [], [], {}
    consumed = set()
    for r1, r2 in zip(rows, rows[1:]):
        for low in r2:
            if not E.is_num(low["text"]):
                continue
            cands = [u for u in r1 if abs(u["cx"] - low["cx"]) < 80 * c._sx
                     and not E.is_num(u["text"])]
            if cands:
                up = min(cands, key=lambda u: abs(u["cx"] - low["cx"]))
                column_kv.append({"label": up["text"], "value": low["text"]})
                consumed.add(id(low))
    for r in rows:
        for j, it in enumerate(r):
            if E.is_num(it["text"]) and j > 0 and id(it) not in consumed:
                prev = r[j - 1]
                if not E.is_num(prev["text"]) and 0 < it["cx"] - prev["cx"] < 280 * c._sx:
                    inline_kv.append({"label": prev["text"], "value": it["text"]})
    for it in items:
        t = it["text"]
        for m in re.finditer(
                r"([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z()（）]{1,12})"
                r"\s*[:：]?\s*([\d,]+(?:\.\d+)?(?:万|亿|%)?)", t):
            embedded.append({"label": m.group(1), "value": m.group(2)})
    print("视觉字段（CDP 后端，%d 块 / %d 行）" % (len(items), len(rows)))
    for kv in column_kv:
        print("  列对齐  %-20s = %s" % (kv["label"], kv["value"]))
    for kv in inline_kv:
        print("  同行    %-20s = %s" % (kv["label"], kv["value"]))
    for kv in embedded[:8]:
        print("  内嵌    %-20s = %s" % (kv["label"], kv["value"]))
    dump("cdp_fields.json", {"time": datetime.now().isoformat(timespec="seconds"),
                             "backend": "cdp", "column_kv": column_kv,
                             "inline_kv": inline_kv, "embedded": embedded, "meta": meta})


# ── 7. 两图差分 ──────────────────────────────────────────────────────────
def cmd_diff(a):
    if not (a.before and a.after):
        print("需要 --before / --after")
        sys.exit(2)
    def load(p):
        im = Image.open(p)
        return E.norm_items(V.ocr(im), a.score)
    b, af = load(a.before), load(a.after)
    added, removed = E.diff_sets(b, af)
    print("前 %d 块 / 后 %d 块  新增 %d / 消失 %d" %
          (len(b), len(af), len(added), len(removed)))
    for t in added[:15]:
        print("   + ", t[:80])
    for t in removed[:15]:
        print("   - ", t[:80])
    dump("cdp_diff.json", {"added": added, "removed": removed,
                           "changed": len(added) + len(removed)})


def main():
    ap = argparse.ArgumentParser(description="CDP 无感化视觉操作后端")
    ap.add_argument("cmd", choices=["test", "map", "click", "type",
                                    "patrol", "fields", "diff"])
    ap.add_argument("--text", default="内容管理", help="要定位/点击的文字")
    ap.add_argument("--hint", help="type: 输入框占位文字")
    ap.add_argument("--input", help="type: 要输入的文字，空表示清空")
    ap.add_argument("--enter", action="store_true")
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--region", help="图内区域 x1,y1,x2,y2（0~1 小数按比例）")
    ap.add_argument("--pick", choices=["first", "bottom", "top", "score"], default="first")
    ap.add_argument("--score", type=float, default=0.45)
    ap.add_argument("--items", default="草稿箱,素材库,发表记录,首页")
    ap.add_argument("--expand", default="内容管理")
    ap.add_argument("--delay", type=float, default=1.6)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--before"), ap.add_argument("--after")
    a = ap.parse_args()
    {"test": cmd_test, "map": cmd_map, "click": cmd_click, "type": cmd_type,
     "patrol": cmd_patrol, "fields": cmd_fields, "diff": cmd_diff}[a.cmd](a)


if __name__ == "__main__":
    main()
