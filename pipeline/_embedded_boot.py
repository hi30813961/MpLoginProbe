# -*- coding: utf-8 -*-
"""MpLoginProbe.exe 内嵌 CPython 的启动胶水（方案 A）。

由 Qt 进程里的 CPython 解释器执行，不修改任何既有 pipeline 模块。
只做四件事：
  1. GUI 程序没有控制台，把 stdout/stderr 落到 embed.log；
  2. cdp_vision / vision_* 顶层 import PIL 只为视觉辅助脚本服务，
     工作台运行时的 Cdp 类完全用不到，注入最小桩避免随包携带 Pillow；
  3. 把 ui_server.run_cli 从「起一个新 Python 进程」换成「当前解释器内
     直接调用 pipeline.main()」——内嵌后 sys.executable 是 Qt exe 自己，
     原路径会去启动 MpLoginProbe.exe，必须替换；
  4. 构造 ThreadingHTTPServer 阻塞运行，靠文件哨兵优雅退出。

退出流程：Qt aboutToQuit 写 .py_stop → 看门狗调 srv.shutdown() →
serve_forever 返回 → 停 overlay 桥 → PyRun_SimpleString 返回 →
Qt 侧 Py_FinalizeEx() → 线程 join。
"""
import io
import os
import sys
import threading
import traceback
import types

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
PORT = int(os.environ.get("MP_ADMIN_PORT", "8766"))
STOP_FILE = os.environ.get("MP_STOP_FILE") or os.path.join(ROOT, ".py_stop")


def _setup_streams():
    """-mwindows 的 GUI 进程里 sys.stdout/stderr 可能是 None，print 会炸。"""
    log_path = os.environ.get("MP_EMBED_LOG") or os.path.join(ROOT, "embed.log")
    try:
        f = open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError:
        f = io.StringIO()
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f
    sys.path.insert(0, BASE)
    sys.path.insert(0, ROOT)
    # 兜底：万一某处仍走 subprocess，指向随包的 python.exe 而不是 Qt 自己
    py_exe = os.path.join(ROOT, "pyruntime", "python.exe")
    if os.path.isfile(py_exe):
        sys.executable = py_exe
    return f


def _ensure_pil():
    """运行时核心只用 cdp_vision.Cdp（纯 HTTP），视觉函数不会被调用。"""
    try:
        import PIL  # noqa: F401
        return "real"
    except ImportError:
        pass
    pil = types.ModuleType("PIL")
    pil.__path__ = []
    for name in ("Image", "ImageDraw", "ImageGrab", "ImageFont"):
        m = types.ModuleType("PIL." + name)
        setattr(pil, name, m)
        sys.modules["PIL." + name] = m
    sys.modules["PIL"] = pil
    return "stub"


def _install_inproc_cli():
    """复用完全相同的 CLI 分派路径，但不再 fork 进程。"""
    import ui_server
    import pipeline
    lock = threading.Lock()

    def run_cli(args, timeout=150):
        with lock:                       # sys.argv/stdout 是全局的，写操作串行
            buf = io.StringIO()
            old = (sys.argv, sys.stdout, sys.stderr)
            code = 0
            try:
                sys.argv = ["pipeline.py"] + [str(x) for x in args]
                sys.stdout = sys.stderr = buf
                pipeline.main()
            except SystemExit as e:
                c = e.code
                if c is None:
                    code = 0
                elif isinstance(c, int):
                    code = c
                else:
                    code = 1
                    print(c)             # SystemExit("❌ ...") 的消息要回传
            except BaseException:
                code = 1
                traceback.print_exc(file=buf)
            finally:
                sys.argv, sys.stdout, sys.stderr = old
            return {"ok": code == 0, "code": code,
                    "output": buf.getvalue().strip()}

    # runpy 以 __main__ 执行本文件，__module__ 不可靠，用显式标记
    run_cli._embedded_inproc = True
    ui_server.run_cli = run_cli
    return ui_server


def _install_selfcheck(ui_server, pil_mode):
    """GET /api/embed-info：一眼看出当前是不是内嵌单进程模式。"""
    def embed_info(cfg):
        return {
            "ok": True,
            "embedded": True,
            "mode": "qt-inproc",
            "python": sys.version.split()[0],
            "pid": os.getpid(),
            "executable": sys.executable,
            "inproc_cli": bool(getattr(ui_server.run_cli, "_embedded_inproc", False)),
            "pil": pil_mode,
            "admin_port": PORT,
        }
    ui_server.GET_API["embed-info"] = embed_info


def main():
    log = _setup_streams()
    pil_mode = _ensure_pil()
    ui_server = _install_inproc_cli()
    _install_selfcheck(ui_server, pil_mode)
    ui_server.PORT = PORT

    cfg = ui_server.load_cfg()
    if not os.path.isdir(ui_server.root_dir(cfg)):
        raise SystemExit("素材目录不存在：" + str(ui_server.root_dir(cfg)))
    ui_server.api_list(cfg)              # 预热 /media 图片白名单

    bridge = None
    try:
        import mp_bridge
        bridge = mp_bridge.start(
            (cfg.get("delivery") or {}).get("cdp", "http://127.0.0.1:8765"),
            PORT)
        print("[管理台] overlay 桥已启用")
    except Exception as e:
        print("[管理台] overlay 桥未启用：%s" % e)

    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), ui_server.Handler)
    srv.daemon_threads = True

    def stop_watch():
        ev = threading.Event()
        while not os.path.exists(STOP_FILE):
            ev.wait(0.30)
        srv.shutdown()

    threading.Thread(target=stop_watch, name="mp-embed-stop", daemon=True).start()
    print("[管理台] 内嵌服务已就绪 http://127.0.0.1:%d/  PIL=%s" % (PORT, pil_mode))
    try:
        srv.serve_forever(poll_interval=0.25)
    finally:
        srv.server_close()
        if bridge is not None:
            try:
                bridge.stop_now()
            except Exception:
                pass
        try:
            os.remove(STOP_FILE)
        except OSError:
            pass
        print("[管理台] 内嵌服务已停止")
        try:
            log.flush()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        try:
            traceback.print_exc(file=sys.stderr)
        except Exception:
            pass
        raise
