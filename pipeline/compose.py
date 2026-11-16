# -*- coding: utf-8 -*-
"""素材 -> 公众号文章 HTML（全内联样式，微信编辑器只认 style 属性，不认 <style>）。

产出三件：
  fragment.html  可直接注入公众号正文编辑器的 HTML 片段
  preview.html   本地预览页（模拟手机宽度，含标题/作者/封面）
  manifest.json  图片待上传清单 + 人工核对项 checks

原则：不编造。价格 / 准确率 / 授权 / 效果等没给的字段一律输出 {{待填}}，
并在 checks 里标 ❌，发布前由 CLI 拦截。
"""
import html
import json
import os
import re
from datetime import datetime

F = "font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Helvetica Neue',Arial,sans-serif;"

# ── 配色：必须同时满足「浅色底」和「微信暗色模式」两种场景的对比度 ──
# 微信的暗色模式会把整篇文章做反色处理，浅灰字 / 亮绿字会直接掉成低对比，
# 编辑器的「内容结构检测」会报 darkmode-low-contrast 并挡住发布。
# 规则：正文级文字一律用 L 在 0.10~0.20 之间的颜色（#1a1a1a ~ #6b7280），
# 品牌绿只用于装饰性色块（左边框、分割线），文字绿一律用深绿 GREEN_TXT。
GREEN = "#07c160"            # 只做装饰：边框、色块、下划线
GREEN_TXT = "#0a7a42"        # 绿字统一用它：白底 5.1:1，反色后仍 5:1 以上
MUTED = "#6b7280"            # 次要文字：白底 4.9:1，替代原来的 #9a9a9a / #a6a6a6

P = ("margin:0 0 18px;padding:0;line-height:1.85;font-size:16px;"
     "color:#3f3f3f;letter-spacing:.3px;text-align:justify;" + F)
H2 = ("margin:34px 0 16px;padding:0 0 0 12px;border-left:4px solid " + GREEN + ";"
      "font-size:19px;font-weight:700;line-height:1.45;color:#1a1a1a;" + F)
BOX = ("margin:22px 0;padding:16px 18px;background:#f7f8fa;border-radius:8px;"
       "border:1px solid #eceef1;" + F)
QUOTE = ("margin:20px 0;padding:12px 16px;background:#fffbe6;"
         "border-left:3px solid #f0c000;color:#6b5b20;line-height:1.8;font-size:15px;" + F)
CAP = ("margin:-10px 0 22px;text-align:center;font-size:13px;color:" + MUTED + ";" + F)
IMG = "display:block;width:100%;max-width:100%;height:auto;margin:20px auto;border-radius:6px;"
PENDING = ("display:block;padding:28px 12px;margin:20px 0;text-align:center;"
           "background:#fff4f4;border:1px dashed #f0a0a0;border-radius:8px;"
           "color:#c0392b;font-size:14px;" + F)

TEMPLATES = {
    "soft": {"label": "销售软文", "lead": True, "cta": True, "toc": False},
    "digest": {"label": "资讯摘要", "lead": True, "cta": False, "toc": True},
    "tutorial": {"label": "教程步骤", "lead": True, "cta": False, "toc": True},
}


def _inline(s):
    """行内 markdown：**粗**、`码`、[文字](链接)。"""
    s = html.escape(s or "", quote=False)
    # 注意：raw string 里不要写 \"，否则会输出字面反斜杠，内联样式在浏览器里失效
    s = re.sub(r"\*\*(.+?)\*\*", r'<strong style="color:#1a1a1a;">\1</strong>', s)
    s = re.sub(r"`(.+?)`", r'<code style="background:#f2f3f5;padding:2px 5px;border-radius:3px;font-size:14px;">\1</code>', s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
               r'<a href="\2" style="color:%s;text-decoration:none;">\1</a>' % GREEN_TXT, s)
    return s


def _img(m, mimg, idx, checks, base_url, cap=""):
    """解析 ![图注](本地路径|URL|数字) 或 {{IMG:n}}。返回 (html, 是否消耗图片)。"""
    src = m.strip()
    if src.isdigit():
        n = int(src)
        src = mimg[n] if n < len(mimg) else ""
    elif re.match(r"^\d+[:：]", src):
        n, cap = re.split(r"[:：]", src, 1)
        n = int(n)
        src = mimg[n] if n < len(mimg) else ""
    if src.startswith("{{IMG:") and src.endswith("}}"):
        n = int(src[6:-2])
        src = mimg[n] if n < len(mimg) else ""
    if not src:
        checks.append("❌ 正文引用了不存在的图片序号：%s" % m)
        return '<span style="%s">{{待补图片：%s}}</span>' % (PENDING, html.escape(m)), False
    if not re.match(r"^(https?|data):", src):
        if base_url:
            src = base_url.rstrip("/") + "/" + os.path.basename(src)
        else:
            checks.append("⚠️ 图片为本地路径，粘贴进微信前需先上传素材库：%s"
                          "（可点界面「上传图片」或跑 upload --id 自动上传并回填）" % src)
            return ('<span style="%s">[待上传图片] %s</span>'
                    % (PENDING, html.escape(os.path.basename(src)))), False
    out = '<img src="%s" style="%s" alt="%s"/>' % (html.escape(src, quote=True), IMG, html.escape(cap))
    if cap:
        out += '<p style="%s">%s</p>' % (CAP, _inline(cap))
    return out, True


def _table_html(rows):
    """把 Markdown 表格行（已拆好 cells）渲染成公众号可用的带样式 <table>。"""
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)
    th = '<th style="border:1px solid #e5e6eb;padding:8px 10px;text-align:left;' \
         'background:#f7f8fa;font-weight:700;color:#1a1a1a;">%s</th>'
    td = '<td style="border:1px solid #e5e6eb;padding:8px 10px;color:#3f3f3f;">%s</td>'
    body = []
    for i, r in enumerate(rows):
        r = r + [""] * (ncol - len(r))
        tag = th if i == 0 else td
        body.append("<tr>%s</tr>" % "".join(tag % _inline(c) for c in r))
    return ('<table style="border-collapse:collapse;width:100%%;margin:0 0 18px;'
            'font-size:15px;line-height:1.7;">%s</table>' % "".join(body))


def md_to_html(body, material, cfg, checks):
    imgs = material.images or []
    base_url = cfg.get("image_base_url", "") or ""
    out, para, consumed = [], [], 0
    table_rows = []

    def flush_table():
        if table_rows:
            out.append(_table_html(list(table_rows)))
            table_rows.clear()

    def flush_para():
        if para:
            out.append('<p style="%s">%s</p>' % (P, _inline(" ".join(para))))
            para.clear()

    def flush():
        flush_table()
        flush_para()

    for line in (body or "").splitlines():
        s = line.strip()
        if not s:
            flush()
            continue
        # Markdown 表格：| a | b | 行；分隔行 |---|---| 跳过
        mcell = re.fullmatch(r"\|(.+)\|", s)
        if mcell:
            flush_para()
            cells = [c.strip() for c in mcell.group(1).split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            table_rows.append(cells)
            continue
        flush_table()
        im = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", s)
        ph = re.fullmatch(r"(\{\{IMG:\d+\}\})", s)
        if im or ph:
            flush()
            key = im.group(2) if im else ph.group(1)
            cap = im.group(1) if im else ""
            h, ok = _img(key, imgs, consumed, checks, base_url, cap=cap)
            if ok:
                consumed += 1
            out.append(h)
            continue
        if s.startswith("## "):
            flush()
            out.append('<h2 style="%s">%s</h2>' % (H2, _inline(s[3:])))
        elif s.startswith("# "):
            flush()
            out.append('<h2 style="%s">%s</h2>' % (H2, _inline(s[2:])))
        elif s.startswith("> "):
            flush()
            out.append('<blockquote style="%s">%s</blockquote>' % (QUOTE, _inline(s[2:])))
        elif re.match(r"^[-*]\s+", s):
            flush()
            out.append('<p style="margin:0 0 10px;padding-left:18px;line-height:1.8;font-size:16px;color:#3f3f3f;%s">'
                       '<span style="color:#0a7a42;font-weight:700;">• </span>%s</p>'
                       % (F, _inline(re.sub(r"^[-*]\s+", "", s))))
        elif re.match(r"^\d+[.、．]\s*", s):
            flush()
            num = re.match(r"^(\d+)[.、．]\s*", s).group(1)
            out.append('<p style="margin:0 0 10px;padding-left:18px;line-height:1.8;font-size:16px;color:#3f3f3f;%s">'
                       '<span style="color:#0a7a42;font-weight:700;">%s. </span>%s</p>'
                       % (F, num, _inline(re.sub(r"^\d+[.、．]\s*", "", s))))
        elif s == "---":
            flush()
            out.append('<p style="margin:28px 0;border-top:1px solid #eceef1;%s"></p>' % F)
        else:
            para.append(s)
    flush()
    return "\n".join(out)


def render(material, cfg):
    tpl = TEMPLATES.get(cfg.get("template", "soft"), TEMPLATES["soft"])
    checks = list(material.validate())
    if checks:
        checks = ["❌ " + c for c in checks]
    if not material.summary:
        checks.append("⚠️ 未填写摘要，微信会自动截取正文前 54 字")
    if not material.cover:
        checks.append("⚠️ 未提供封面图，发布前需在编辑器里手动设置")
    if not material.author:
        checks.append("⚠️ 未填写作者")

    body = material.extra.get("body_html") or md_to_html(
        material.body_md, material, cfg, checks)

    lead = ""
    if tpl["lead"] and material.summary:
        lead = ('<section style="%s"><p style="margin:0;line-height:1.8;'
                'font-size:15px;color:#5b6b73;%s">%s</p></section>'
                % (BOX, F, _inline(material.summary)))

    tags = ""
    if material.tags:
        tags = '<p style="margin:26px 0 0;%s">%s</p>' % (
            CAP, "　".join('<span style="display:inline-block;padding:3px 10px;'
                          'margin-right:6px;background:#eaf6ef;color:' + GREEN_TXT + ';'
                          'border-radius:12px;font-size:12px;">#%s</span>'
                          % html.escape(str(t)) for t in material.tags))

    cta = ""
    if tpl["cta"]:
        txt = (cfg.get("cta", "") or "").strip()
        if txt:
            cta = ('<section style="margin:32px 0 8px;padding:20px;background:#f2f8f4;'
                   'border-radius:10px;text-align:center;%s">'
                   '<p style="margin:0 0 8px;font-size:15px;font-weight:700;color:#0a7a42;">'
                   '想了解更多？</p>'
                   '<p style="margin:0;font-size:15px;color:#3f7a58;line-height:1.8;">%s</p>'
                   '</section>' % (F, _inline(txt)))

    footer = ""
    if cfg.get("footer"):                # 默认不带页脚，config 显式开启才输出
        footer = ('<p style="margin:30px 0 0;padding-top:16px;border-top:1px solid #eceef1;'
                  'font-size:12px;color:%s;text-align:center;%s">'
                  '本文由素材库自动排版生成 · %s</p>'
                  % (MUTED, F, datetime.now().strftime("%Y-%m-%d")))

    fragment = "\n".join(x for x in (lead, body, tags, cta, footer) if x)
    preview = _wrap_preview(material, fragment, tpl["label"], checks)
    manifest = {
        "id": material.id,
        "title": material.title,
        "summary": material.summary,
        "author": material.author,
        "cover": material.cover,
        "template": tpl["label"],
        "images": material.images,
        "pending_upload": [x for x in material.images
                           if x and not re.match(r"^(https?|data):", x)],
        "source": material.source,
        "checks": checks,
        "has_blocker": any(c.startswith("❌") for c in checks),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return {"fragment": fragment, "preview": preview, "manifest": manifest}


def _wrap_preview(m, fragment, label, checks):
    cover = ""
    if m.cover and re.match(r"^(https?|data):", m.cover):
        cover = '<img src="%s" style="width:100%%;display:block;"/>' % html.escape(m.cover, quote=True)
    bar = ""
    if checks:
        bar = ('<div style="background:#fffbe6;border-bottom:1px solid #f0e2a0;'
               'padding:10px 16px;font-size:13px;color:#8a6d3b;%s">'
               '待核对 %d 项：%s</div>'
               % (F, len(checks), "<br/>".join(html.escape(c) for c in checks)))
    return ("""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>%s - 预览</title></head>
<body style="margin:0;background:#e9ebee;%s">
<div style="max-width:420px;margin:0 auto;background:#fff;min-height:100vh;">
<div style="padding:8px 16px;background:#fafafa;font-size:12px;color:#999;%s">本地预览 · 模板 %s · 非微信实际渲染</div>
%s%s
<div style="padding:22px 18px 60px;">
<h1 style="margin:0 0 12px;font-size:23px;line-height:1.4;font-weight:700;color:#1a1a1a;%s">%s</h1>
<p style="margin:0 0 24px;font-size:13px;color:#9a9a9a;%s">%s　%s</p>
%s
</div></div></body></html>""" % (
        html.escape(m.title), F, F, label, bar, cover,
        F, html.escape(m.title), F, html.escape(m.author or "未填作者"),
        datetime.now().strftime("%Y-%m-%d"), fragment))


def slug(mid):
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5_-]+", "-", str(mid)).strip("-")
    return (s or "article")[:60]


def write_out(result, out_dir):
    mid = result["manifest"]["id"]
    d = os.path.join(out_dir, slug(mid))
    os.makedirs(d, exist_ok=True)
    paths = {}
    for name, key in (("fragment.html", "fragment"), ("preview.html", "preview")):
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(result[key])
        paths[key] = p
    p = os.path.join(d, "manifest.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(result["manifest"], f, ensure_ascii=False, indent=2)
    paths["manifest"] = p
    return paths
