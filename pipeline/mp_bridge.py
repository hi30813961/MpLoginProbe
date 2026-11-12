# -*- coding: utf-8 -*-
"""公众号后台「素材库」页面增强面板的本机桥。

注入脚本 mp_overlay.js 只负责界面；它把用户点击放进 window.__mpui 队列，
桥通过已验证的 CDP 回环通道（127.0.0.1:8765）轮询取回、执行、回写结果。
页面内不发起任何外部请求，因此不受 CORS / 混合内容 / 私有网络策略影响。

安全边界：
  - 只向页面追加一个 shadow DOM 容器和一个工具栏按钮，不改官方元素与请求；
  - 动作白名单只有刷新/生成预览/存草稿/复制排版/看预览/发布指引/回写台账/打开本地目录；
  - 不上传、不删除官方素材，不点「发表/群发」；草稿模式闸门由 pipeline.py 保持。
"""
import json
import os
import sys
import threading
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(BASE))

from cdp_vision import Cdp          # noqa: E402
import deliver                       # noqa: E402

SCRIPT_PATH = os.path.join(BASE, "mp_overlay.js")
LOG_PATH = os.path.join(BASE, "overlay.log")
CFG_PATH = os.path.join(BASE, "config.json")
DEFAULT_CDP = "http://127.0.0.1:8765"
EXPECT_V = 4          # 面板脚本版本；升级时强制重注入

_BRIDGE = None
_LOCK = threading.Lock()


def js_lit(obj):
    """纯 ASCII 的 JS 字面量，避免中文/换行破坏 Runtime.evaluate 表达式。"""
    return json.dumps(obj, ensure_ascii=True)


class Bridge(object):
    def __init__(self, cdp_url=DEFAULT_CDP, local_port=8766, interval=1.5):
        self.cdp_url = cdp_url
        self.local_port = local_port
        self.interval = interval
        self.script = open(SCRIPT_PATH, encoding="utf-8").read()
        self.script_mtime = os.path.getmtime(SCRIPT_PATH)
        self.last_sig = None
        self.stop = threading.Event()
        self.thread = None
        self.state = "starting"
        self.probe_state = ""
        self.url = ""
        self.installs = 0
        self.actions = 0
        self.last_render = 0
        self.last_error = ""
        self.ticks = 0

    # ── 配置 / 日志 ───────────────────────────────────────────────────────
    def cfg(self):
        p = CFG_PATH if os.path.exists(CFG_PATH) else os.path.join(BASE, "config.example.json")
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def log(self, text):
        line = "%s  %s" % (datetime.now().strftime("%H:%M:%S"), text)
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def route(self):
        u = self.url
        if "appmsg_edit" in u or "action=edit" in u:
            return "editor"
        if "filepage" in u:
            return "material"
        if "appmsgpublish" in u:
            return "published"
        if "/cgi-bin/appmsg?" in u and ("action=list" in u or "list_card" in u):
            return "draft"
        return "other"

    def info(self):
        return {"ok": True, "state": self.state, "probe": self.probe_state,
                "url": self.url[:160], "injections": self.installs,
                "script_v": EXPECT_V, "route": self.route(),
                "actions": self.actions, "ticks": self.ticks,
                "last_error": self.last_error[:160],
                "last_render": datetime.fromtimestamp(self.last_render).strftime("%H:%M:%S")
                if self.last_render else ""}

    # ── CDP ───────────────────────────────────────────────────────────────
    def ev(self, c, expr):
        return deliver.ev(c, expr)

    def ensure_installed(self, c):
        if self.ev(c, "document.readyState") != "complete":
            self.state = "loading"
            return False
        try:
            mtime = os.path.getmtime(SCRIPT_PATH)
        except OSError:
            mtime = self.script_mtime
        v = self.ev(c, "window.__mpui && window.__mpui.v || 0")
        # 版本升级、或脚本文件被改动 -> 热重注入（无需重启 UI 服务）
        if v != EXPECT_V or mtime != self.script_mtime:
            try:
                with open(SCRIPT_PATH, encoding="utf-8") as f:
                    self.script = f.read()
                self.script_mtime = mtime
            except OSError:
                pass
            # 清掉旧引用，绕过新脚本开头的同版本早退
            self.ev(c, "(function(){try{delete window.__mpui}catch(e){window.__mpui=undefined}"
                    "return window.__mpui&&window.__mpui.v||0})()")
            self.ev(c, self.script)
            v = self.ev(c, "window.__mpui && window.__mpui.v || 0")
            if v != EXPECT_V:
                raise RuntimeError("注入后面板未就绪 (v=%r, 期望 %d)" % (v, EXPECT_V))
            self.installs += 1
            self.last_sig = None
            self.log("已注入增强面板 v%d，第 %d 次" % (EXPECT_V, self.installs))
            self.push_render(c, force=True)
        self.state = "injected"
        return True

    def cached_drafts(self):
        """本机已解析的后台草稿（parsed/drafts.json），供草稿箱视图做只读展示。"""
        p = os.path.join(os.path.dirname(BASE), "parsed", "drafts.json")
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            return [{"title": x.get("title") or "(无标题)",
                     "create_time": x.get("create_time") or "",
                     "update_time": x.get("update_time") or ""}
                    for x in (d.get("items") or [])[:5]]
        except (OSError, ValueError, AttributeError):
            return []

    def snapshot(self):
        import ui_server
        cfg = self.cfg()
        ov = ui_server.api_overview(cfg)
        lst = ui_server.api_list(cfg)
        pending = []
        out_root = ui_server.out_root(cfg)
        if os.path.isdir(out_root):
            for name in sorted(os.listdir(out_root)):
                p = os.path.join(out_root, name, "manifest.json")
                if not os.path.isfile(p):
                    continue
                try:
                    with open(p, encoding="utf-8") as f:
                        mf = json.load(f)
                    pending.extend(mf.get("pending_upload") or [])
                except (OSError, ValueError):
                    pass
        return {
            "ok": True,
            "mode": ov["mode"], "total": ov["total"],
            "published_today": ov["published_today"],
            "probe": (ov.get("probe") or {}).get("state") or self.probe_state,
            "items": [{"id": x["id"], "title": x["title"],
                       "status": x["status"], "body_len": x["body_len"],
                       "images": len(x.get("images") or [])}
                      for x in lst["items"]],
            "pending_upload": list(dict.fromkeys(pending))[:12],
            "cached_drafts": self.cached_drafts(),
            "server_time": ov["server_time"],
        }

    def push_render(self, c, force=False):
        """数据没变就不下发，避免面板无谓重绘吞掉用户点击。"""
        data = self.snapshot()
        sig_data = dict(data)
        sig_data.pop("server_time", None)
        sig = json.dumps(sig_data, sort_keys=True, ensure_ascii=False)
        if not force and sig == self.last_sig:
            return
        self.ev(c, "window.__mpui.render(" + js_lit(data) + ")")
        self.last_sig = sig
        self.last_render = time.time()

    def push_result(self, c, seq, res):
        expr = "window.__mpui && window.__mpui.onResult(%d, %s)" % (int(seq), js_lit(res))
        for i in range(5):
            try:
                if self.ev(c, "window.__mpui && window.__mpui.v || 0") == EXPECT_V:
                    self.ev(c, expr)
                    return True
            except Exception:
                pass
            time.sleep(0.8)
            try:
                self.ensure_installed(c)     # draft 会导航，面板需重新注入
            except Exception:
                pass
        return False

    # ── 动作白名单 ────────────────────────────────────────────────────────
    def run_action(self, cmd, args):
        import ui_server
        cfg = self.cfg()
        mid = str(args.get("id") or "")
        if cmd == "refresh":
            return {"ok": True, "message": "已刷新本机素材数据"}
        if not mid and cmd in ("build", "draft", "copy", "preview",
                               "guide", "published"):
            return {"ok": False, "error": "缺少素材 id"}
        if cmd == "build":
            r = ui_server.api_build(cfg, {"id": mid})
            return {"ok": True, "message": r["message"]}
        if cmd == "draft":
            r = ui_server.run_cli(["draft", "--id", mid])
            return {"ok": bool(r.get("ok")),
                    "output": (r.get("output") or r.get("error") or "无输出")[-700:]}
        if cmd == "copy":
            ui_server.api_build(cfg, {"id": mid})
            r = ui_server.api_copy(cfg, {"id": mid})
            return {"ok": True, "message": r["message"]}
        if cmd == "preview":
            p = ui_server.out_path(cfg, mid, "preview.html")
            if not os.path.isfile(p):
                return {"ok": False, "error": "还没有预览页，先点「生成预览」"}
            os.startfile(p)
            return {"ok": True, "message": "已用默认浏览器打开预览页"}
        if cmd == "guide":
            r = ui_server.run_cli(["publish", "--id", mid])
            return {"ok": bool(r.get("ok")),
                    "output": (r.get("output") or r.get("error") or "无输出")[-700:]}
        if cmd == "published":
            r = ui_server.run_cli(["published", "--id", mid])
            return {"ok": bool(r.get("ok")),
                    "output": (r.get("output") or r.get("error") or "无输出")[-700:]}
        if cmd == "openlocal":
            os.startfile("http://127.0.0.1:%d" % self.local_port)
            return {"ok": True, "message": "已用默认浏览器打开本地管理页"}
        if cmd == "reveal":
            os.startfile(ui_server.root_dir(cfg))
            return {"ok": True, "message": "已打开素材目录"}
        return {"ok": False, "error": "未授权的动作：" + str(cmd)}

    # ── 主循环 ────────────────────────────────────────────────────────────
    def tick(self):
        c = Cdp(self.cdp_url)
        s = c.status()
        self.probe_state = str(s.get("state") or "")
        self.url = str(s.get("url") or "")
        self.ticks += 1
        if "mp.weixin.qq.com" not in self.url:
            self.state = "waiting_page"
            return
        if not self.ensure_installed(c):
            return
        try:
            self.ev(c, "window.__mpui.ensureButton()")
        except Exception:
            pass
        raw = self.ev(c, "window.__mpui.take()")
        try:
            queue = json.loads(raw or "[]")
        except ValueError:
            queue = []
        for item in queue[:5]:
            seq, cmd, args = item.get("seq"), item.get("cmd"), item.get("args") or {}
            self.log("执行 %s %s" % (cmd, args.get("id", "")))
            try:
                res = self.run_action(cmd, args)
            except Exception as e:
                res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            self.actions += 1
            self.push_result(c, seq, res)
            self.log("结果 %s -> %s" % (cmd, "ok" if res.get("ok") else "fail"))
            if cmd not in ("refresh", "openlocal", "reveal", "preview"):
                self.push_render(c, force=True)   # 台账/产物变了，立刻回显
        if self.ticks % 6 == 1 or not self.last_render:
            self.push_render(c)

    def loop(self):
        self.log("桥已启动（间隔 %.1fs）" % self.interval)
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:
                self.state = "error"
                self.last_error = "%s: %s" % (type(e).__name__, str(e)[:200])
            self.stop.wait(self.interval)

    def start(self):
        self.thread = threading.Thread(target=self.loop, name="mp-overlay-bridge", daemon=True)
        self.thread.start()
        return self

    def stop_now(self):
        self.stop.set()


def start(cdp_url=DEFAULT_CDP, local_port=8766, interval=1.5):
    global _BRIDGE
    with _LOCK:
        if _BRIDGE is None:
            _BRIDGE = Bridge(cdp_url, local_port, interval).start()
        return _BRIDGE


def info():
    return _BRIDGE.info() if _BRIDGE else {"ok": False, "state": "disabled"}


if __name__ == "__main__":
    b = start()
    print("公众号后台增强面板桥已启动：http://127.0.0.1:%d/api/overlay" % b.local_port)
    print("Ctrl+C 停止")
    try:
        while True:
            time.sleep(3)
            print(json.dumps(b.info(), ensure_ascii=False))
    except KeyboardInterrupt:
        b.stop_now()
