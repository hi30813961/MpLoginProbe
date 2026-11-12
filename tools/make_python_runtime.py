# -*- coding: utf-8 -*-
"""生成随 Qt exe 分发的精简 CPython 运行时（方案 A：内嵌 CPython）。

用法：
    python tools/make_python_runtime.py                 # 自动用当前解释器
    python tools/make_python_runtime.py --python C:/Python313
    python tools/make_python_runtime.py --out dist/pyruntime

产出目录（默认 <项目根>/pyruntime）：
    python313.dll / python3.dll / vcruntime140*.dll
    python.exe           兜底：嵌入模式失败或显式要求子进程时使用
    python313.zip        精简标准库（不含 site-packages / __pycache__ / 测试包）
    DLLs/                扩展模块 .pyd 及其依赖的 dll

设计原则：只携带「标准库 + 扩展模块」，不携带任何第三方包。
pipeline/ 业务代码零改动，仍作为真实文件放在原目录，由 PYTHONPATH 引入。
"""
import argparse
import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 标准库中体积大且工作台绝不会用到的部分
SKIP_DIRS = {
    "site-packages", "__pycache__", "test", "tests", "idlelib", "tkinter",
    "turtledemo", "ensurepip", "venv", "pydoc_data", "_pyrepl", "lib2to3",
    "distutils", "curses", "html5lib", "unittest",
}
SKIP_FILE_PREFIX = ("test_", "test.", "turtle", "antigravity", "this.py")
# 扩展模块：仅剔除纯 CPython 自测用的，其余全部保留以保证可靠
SKIP_PYD_PREFIX = ("_test", "_ctypes_test")


def build_zip(lib_dir, zip_path):
    n = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, dirs, files in os.walk(lib_dir):
            rel_root = os.path.relpath(root, lib_dir)
            parts = set(rel_root.split(os.sep)) if rel_root != "." else set()
            dirs[:] = [d for d in dirs
                       if d not in SKIP_DIRS and not d.endswith(".dist-info")]
            if parts & SKIP_DIRS:
                continue
            for fn in files:
                if fn.startswith(SKIP_FILE_PREFIX):
                    continue
                if not fn.endswith((".py", ".pyc", ".pyd", ".dll", ".txt", ".json")):
                    continue
                if fn.endswith(".pyc") and "__pycache__" not in root:
                    continue
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, lib_dir).replace(os.sep, "/")
                # __pycache__ 下的 .pyc 没有对应的 .py 也保留，但路径要是模块路径
                if "__pycache__" in arc:
                    continue
                try:
                    z.write(full, arc)
                    n += 1
                except OSError:
                    pass
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.base_prefix, help="源 Python 安装根目录")
    ap.add_argument("--out", default=os.path.join(ROOT, "pyruntime"))
    args = ap.parse_args()

    src = os.path.abspath(args.python)
    out = os.path.abspath(args.out)
    dll_name = "python%d%d.dll" % sys.version_info[:2]
    if not os.path.isfile(os.path.join(src, dll_name)):
        raise SystemExit("找不到 %s，--python 指向的目录不对：%s" % (dll_name, src))

    os.makedirs(out, exist_ok=True)
    os.makedirs(os.path.join(out, "DLLs"), exist_ok=True)

    # 1) 主 DLL + VC runtime + 兜底 python.exe
    mains = [dll_name, "python3.dll", "vcruntime140.dll",
             "vcruntime140_1.dll", "python.exe", "pythonw.exe"]
    copied = []
    for fn in mains:
        s = os.path.join(src, fn)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(out, fn))
            copied.append(fn)

    # 2) 扩展模块
    n_pyd = 0
    src_dlls = os.path.join(src, "DLLs")
    for fn in sorted(os.listdir(src_dlls)):
        if not fn.lower().endswith((".pyd", ".dll")):
            continue
        if fn.startswith(SKIP_PYD_PREFIX):
            continue
        shutil.copy2(os.path.join(src_dlls, fn), os.path.join(out, "DLLs", fn))
        n_pyd += 1

    # 3) 精简标准库 zip
    zip_path = os.path.join(out, "python%d%d.zip" % sys.version_info[:2])
    n_py = build_zip(os.path.join(src, "Lib"), zip_path)

    total = sum(os.path.getsize(os.path.join(r, f))
                for r, _, fs in os.walk(out) for f in fs)
    print("运行时目录：", out)
    print("主文件    ：", ", ".join(copied))
    print("扩展模块  ：", n_pyd, "个")
    print("标准库    ：", n_py, "个文件 ->", os.path.basename(zip_path),
          "%.1f MB" % (os.path.getsize(zip_path) / 1048576.0))
    print("总体积    ： %.1f MB" % (total / 1048576.0))
    print("\nQt 侧需要设置：")
    print("  PYTHONHOME =", out)
    print("  PYTHONPATH = %s;%s;%s" % (
        zip_path, os.path.join(out, "DLLs"), os.path.join(ROOT, "pipeline")))


if __name__ == "__main__":
    main()
