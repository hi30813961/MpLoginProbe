# -*- coding: utf-8 -*-
"""OCR 视觉定位 + 点击闭环（可作用于任意 Windows 窗口，无需 DOM / 选择器）。

流程：窗口截图 -> POST 内网 OCR -> 按文字找 bbox -> 算中心点 -> 真实鼠标点击
     -> （可选）再截图验证。

用法：
  python vision_click.py --title "公众号后台会话探针" --text "导出会话快照" --dry-run
  python vision_click.py --title "公众号后台会话探针" --text "导出会话快照" --verify
  python vision_click.py --screen --text "草稿箱" --dry-run        # 全屏模式
  python vision_click.py --image xxx.png --text "按钮" --dry-run    # 只识别已有图片

安全：默认 --dry-run 只识别不点击；真正点击必须显式去掉 --dry-run。
"""
import argparse
import ctypes
import io
import json
import os
import sys
import time
import urllib.request
from ctypes import wintypes

from PIL import ImageGrab, ImageDraw

BASE = os.path.dirname(os.path.abspath(__file__))

# 凭据与内网地址不写进源码：优先环境变量，其次同目录 ocr.local.json（已在 .gitignore）
# 例：{"url": "http://<你的OCR服务>/api/ocr", "token": "<你的令牌>"}
def _local_conf(key, fallback=""):
    p = os.path.join(BASE, "ocr.local.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return str(json.load(f).get(key) or fallback)
        except Exception:
            pass
    return fallback

OCR_URL = os.environ.get("MP_OCR_URL") or _local_conf("url", "http://127.0.0.1:8756/api/ocr")
OCR_TOKEN = os.environ.get("MP_OCR_TOKEN") or _local_conf("token", "")
TIMEOUT = 60

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()


# ── 窗口 ────────────────────────────────────────────────────────────────
def find_window(substr):
    found = []
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if substr.lower() in buf.value.lower():
            found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(CB(cb), 0)
    return found


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def window_rect(hwnd):
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def grab_window(hwnd, save=None):
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)                     # 等窗口绘制完成
    l, t, r, b = window_rect(hwnd)
    # 裁到屏幕物理范围：超出屏幕的部分抓出来是黑图，会干扰 OCR
    sw = user32.GetSystemMetrics(0)   # SM_CXSCREEN
    sh = user32.GetSystemMetrics(1)   # SM_CYSCREEN
    l, t, r, b = max(l, 0), max(t, 0), min(r, sw), min(b, sh)
    # 屏幕 DC 抓取：GPU 合成 / WebView2 内容也能拿到，PrintWindow 会黑屏
    img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
    if save:
        img.save(save)
    return img, (l, t)


# ── OCR ─────────────────────────────────────────────────────────────────
def ocr(img):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    data = buf.getvalue()
    req = urllib.request.Request(
        OCR_URL, data=data,
        headers={"X-Api-Token": OCR_TOKEN,
                 "Content-Type": "application/octet-stream"},
        method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def center_of(box):
    """box = [x1,y1, x2,y2, x3,y3, x4,y4]（左上/右上/右下/左下）"""
    xs = box[0::2]
    ys = box[1::2]
    return (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0


# 日志/正文里常出现与按钮同名的文字，这些特征用来排除「不是控件」的候选
LOG_MARKS = ("…", "...", "HTTP", "[", "]", "ret", "B  ", "：", ":")


def find_items(result, text, min_score=0.5, filter_log=True, pick="bottom"):
    hits = []
    for it in result.get("items", []):
        t = it.get("text", "")
        if text not in t or it.get("score", 0) < min_score:
            continue
        if filter_log and any(m in t for m in LOG_MARKS):
            continue
        hits.append(it)
    if pick == "bottom":                       # 按钮行通常在窗口下部
        hits.sort(key=lambda i: -center_of(i["box"])[1])
    elif pick == "top":
        hits.sort(key=lambda i: center_of(i["box"])[1])
    elif pick == "score":
        hits.sort(key=lambda i: -i.get("score", 0))
    return hits


def annotate(img, result, target=None):
    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out)
    for it in result.get("items", []):
        box = it.get("box", [])
        if len(box) < 8:
            continue
        xs, ys = box[0::2], box[1::2]
        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        hit = target and target in it.get("text", "")
        color = "#E53935" if hit else "#888888"
        d.rectangle((x1, y1, x2, y2), outline=color, width=3 if hit else 1)
        if hit:
            cx, cy = center_of(box)
            d.ellipse((cx-5, cy-5, cx+5, cy+5), fill="#E53935")
    return out


# ── 点击 ────────────────────────────────────────────────────────────────
def click(x, y, restore=True):
    old = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(old))
    user32.SetCursorPos(int(round(x)), int(round(y)))
    time.sleep(0.12)
    user32.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
    time.sleep(0.06)
    user32.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP
    if restore:
        time.sleep(0.15)
        user32.SetCursorPos(old.x, old.y)


def run_flow(args, keywords):
    """多步操作：每一步都重新截图 → OCR → 定位 → 点击，自动适应上一步造成的界面变化。"""
    wins = find_window(args.title)
    if not wins:
        print("未找到窗口:", args.title)
        sys.exit(2)
    hwnd, title = wins[0]
    print("窗口:", title)
    for i, kw in enumerate(keywords, 1):
        img, origin = grab_window(hwnd)
        res = ocr(img)
        if not res.get("ok"):
            print("  STEP %d OCR 失败: %s" % (i, res))
            return
        hits = find_items(res, kw, args.score,
                          filter_log=not args.no_filter, pick=args.pick)
        if not hits:
            print("  STEP %d ❌ 未找到 %r（本步 %d 个文字块）" % (i, kw, len(res.get("items", []))))
            return
        cx, cy = center_of(hits[0]["box"])
        sx, sy = origin[0] + cx, origin[1] + cy
        print("  STEP %d ✅ %-14r score=%.3f  屏幕(%.0f, %.0f)  %s"
              % (i, kw, hits[0]["score"], sx, sy,
                 "dry-run" if args.dry_run else "点击"))
        if not args.dry_run:
            click(sx, sy)
        time.sleep(args.delay)
    print("流程结束，共 %d 步" % len(keywords))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="公众号后台会话探针", help="窗口标题模糊匹配")
    ap.add_argument("--image", help="直接识别已有图片（不截图）")
    ap.add_argument("--screen", action="store_true", help="截全屏而非指定窗口")
    ap.add_argument("--text", help="要定位的界面文字（子串匹配）")
    ap.add_argument("--index", type=int, default=0, help="多个命中时选第几个")
    ap.add_argument("--score", type=float, default=0.5, help="最低置信度")
    ap.add_argument("--pick", choices=["bottom", "top", "first", "score"],
                    default="bottom", help="多个同名命中时的消歧策略（默认取最靠下的控件）")
    ap.add_argument("--no-filter", action="store_true",
                    help="不过滤日志/正文特征（目标本身含标点时用）")
    ap.add_argument("--dry-run", action="store_true", help="只识别并标注，不点击")
    ap.add_argument("--verify", action="store_true", help="点击后再次截图 OCR")
    ap.add_argument("--out", default=os.path.join(BASE, "vision_shot.png"))
    ap.add_argument("--steps", help="多步操作，逗号分隔，如 '内容管理,草稿箱'")
    ap.add_argument("--delay", type=float, default=1.2, help="多步之间的等待秒数")
    args = ap.parse_args()

    if args.steps:
        run_flow(args, [s.strip() for s in args.steps.split(",") if s.strip()])
        return
    if not args.text:
        ap.error("需要 --text 或 --steps")

    origin = (0, 0)
    if args.image:
        from PIL import Image
        img = Image.open(args.image)
        print("读取图片:", args.image, img.size)
    elif args.screen:
        img = ImageGrab.grab(all_screens=True)
        print("全屏截图:", img.size)
    else:
        wins = find_window(args.title)
        if not wins:
            print("未找到标题包含 '%s' 的可见窗口" % args.title)
            print("当前可见窗口（前 10 个）：")
            for h, t in find_window("")[:10]:
                print("   ", t[:70])
            sys.exit(2)
        hwnd, title = wins[0]
        print("窗口: %s  hwnd=%s" % (title, hwnd))
        img, origin = grab_window(hwnd, save=args.out)
        print("窗口截图:", img.size, " 屏幕原点:", origin)

    res = ocr(img)
    if not res.get("ok"):
        print("OCR 失败:", res)
        sys.exit(3)
    print("OCR: %d 个文字块 / %.0f ms" % (len(res.get("items", [])), res.get("ms", 0)))
    print("--- 识别内容 ---")
    for i, it in enumerate(res.get("items", [])):
        cx, cy = center_of(it["box"])
        print("  [%2d] %-34s score=%.3f  center=(%6.1f, %6.1f)"
              % (i, it["text"][:34], it["score"], cx, cy))

    hits = find_items(res, args.text, args.score,
                      filter_log=not args.no_filter, pick=args.pick)
    if not hits:
        print("未找到包含 '%s' 的文字块（可放宽 --score 或换个关键词）" % args.text)
        annotate(img, res, args.text).save(os.path.join(BASE, "vision_annotated.png"))
        sys.exit(4)
    if args.index >= len(hits):
        print("只有 %d 个命中，--index %d 越界" % (len(hits), args.index))
        sys.exit(4)
    print("候选 %d 个（消歧策略 %s）:" % (len(hits), args.pick))
    for i, h in enumerate(hits[:8]):
        hx, hy = center_of(h["box"])
        print("   [%d] %-30r score=%.3f  center=(%.0f, %.0f)"
              % (i, h["text"][:30], h["score"], hx, hy))
    hit = hits[args.index]
    cx, cy = center_of(hit["box"])
    screen_x, screen_y = origin[0] + cx, origin[1] + cy
    annotate(img, res, args.text).save(os.path.join(BASE, "vision_annotated.png"))
    print("命中: %r  score=%.3f" % (hit["text"], hit["score"]))
    print("图内中心: (%.1f, %.1f)  →  屏幕坐标: (%.1f, %.1f)" % (cx, cy, screen_x, screen_y))
    print("标注图:", os.path.join(BASE, "vision_annotated.png"))

    if args.dry_run:
        print("dry-run：未点击")
        return
    if args.image:
        print("--image 模式不做点击（没有对应窗口坐标）")
        return
    click(screen_x, screen_y)
    print("已点击 (%.0f, %.0f)" % (screen_x, screen_y))

    if args.verify:
        time.sleep(1.8)
        if args.screen:
            img2 = ImageGrab.grab(all_screens=True)
        else:
            img2, _ = grab_window(hwnd)
        res2 = ocr(img2)
        print("点击后画面文字（前 12 条）:")
        for it in res2.get("items", [])[:12]:
            print("   ", it["text"][:60])
        img2.save(os.path.join(BASE, "vision_after.png"))


if __name__ == "__main__":
    main()
