# -*- coding: utf-8 -*-
"""公众号排版对比度体检。

微信编辑器的「内容结构检测」会按 WCAG 算内联色对比度，并额外模拟暗色模式
（整篇反色）再算一次，低于 4.5:1 就报 darkmode-low-contrast，直接卡住发布。

用法：python tools/contrast_check.py out/ai-compute-1115/fragment.html
退出码：0 全部通过；1 有低于阈值的项。
"""
import io
import re
import sys

THRESHOLD = 4.5          # 正文阈值（大字号 3.0，这里统一按严格值卡）


def lum(hexcolor):
    h = hexcolor.strip().lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        rgb = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    except ValueError:
        return None
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def ratio(a, b):
    la, lb = lum(a), lum(b)
    if la is None or lb is None:
        return None
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def invert(hexcolor):
    """微信暗色模式的效果近似：对亮度做反色。"""
    h = hexcolor.strip().lstrip('#')
    if len(h) != 6:
        return hexcolor
    rgb = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    # 用 HSL 方式反相亮度，色相保留，避免出现奇怪的互补色
    r, g, b = [c / 255.0 for c in rgb]
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    nl = 1 - l
    if mx == mn:
        nv = int(round(nl * 255))
        return '#%02x%02x%02x' % (nv, nv, nv)
    d = mx - mn
    s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == r:
        hh = ((g - b) / d) % 6
    elif mx == g:
        hh = (b - r) / d + 2
    else:
        hh = (r - g) / d + 4
    hh *= 60
    c = (1 - abs(2 * nl - 1)) * s
    x = c * (1 - abs((hh / 60) % 2 - 1))
    m = nl - c / 2
    seg = int(hh // 60) % 6
    rr, gg, bb = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][seg]
    return '#%02x%02x%02x' % tuple(int(round(v * 255)) for v in (rr + m, gg + m, bb + m))


def pick(style, prop):
    m = re.search(prop + r'\s*:\s*([#0-9a-zA-Z\(\),\.\s%]+?)\s*(?:;|$)', style)
    return m.group(1).strip() if m else None


def walk(html, inherited_color, inherited_bg, out):
    """粗糙但够用的顺序扫描：按标签流式跟踪 color / background 的继承。"""
    stack = []
    pos = 0
    for m in re.finditer(r'<(/?)([a-zA-Z0-9]+)([^>]*)>|([^<]+)', html):
        if m.group(4) is not None:      # 文本
            text = m.group(4)
            if text.strip():
                out.append((inherited_color, inherited_bg, text.strip()[:40]))
            continue
        closing, tag, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if closing:
            if stack:
                inherited_color, inherited_bg = stack.pop()
            continue
        sm = re.search(r'style\s*=\s*"([^"]*)"', attrs)
        style = sm.group(1) if sm else ''
        color = pick(style, 'color') or inherited_color
        bg = (pick(style, r'background(?:-color)?') or inherited_bg)
        out.append(('#BOUNDARY', '#ffffff', '<' + tag + '>'))
        stack.append((inherited_color, inherited_bg))
        inherited_color, inherited_bg = color, bg
        if tag in ('br', 'img', 'input', 'hr'):
            inherited_color, inherited_bg = stack.pop()


def main():
    if len(sys.argv) < 2:
        print('用法：python tools/contrast_check.py out/<id>/fragment.html')
        return 2
    src = io.open(sys.argv[1], encoding='utf-8').read()
    # 逐个块检查：块自身的 color/background 组合 + 块内 span 的组合
    blocks = list(re.finditer(r'<(p|h1|h2|h3|section|blockquote|li)\b([^>]*)>', src))
    bad = 0
    total = 0
    for i, b in enumerate(blocks, 1):
        sm = re.search(r'style\s*=\s*"([^"]*)"', b.group(2))
        style = sm.group(1) if sm else ''
        color = pick(style, 'color') or '#3f3f3f'
        bg = pick(style, r'background(?:-color)?') or '#ffffff'
        pairs = [(color, bg, '块本身')]
        # 块内联元素的独立配色
        end = src.find('</' + b.group(1), b.end())
        inner = src[b.end():end if end > 0 else len(src)]
        for sp in re.finditer(r'<(\w+)([^>]*style="([^"]*)"[^>]*)>', inner):
            sc = pick(sp.group(3), 'color')
            sb = pick(sp.group(3), r'background(?:-color)?')
            if sc or sb:
                pairs.append((sc or color, sb or bg, '<' + sp.group(1) + '>'))
        for fg, bgr, who in pairs:
            if not fg.startswith('#') or not bgr.startswith('#'):
                continue
            total += 1
            r = ratio(fg, bgr)
            rd = ratio(invert(fg), invert(bgr))
            flag = ''
            if r is not None and r < THRESHOLD:
                flag += ' 浅色不足'
            if rd is not None and rd < THRESHOLD:
                flag += ' 暗色不足'
            if flag:
                bad += 1
                print('第 %d 块 %s %s/%s  浅色 %.2f  暗色 %.2f  ←%s'
                      % (i, who, fg, bgr, r or 0, rd or 0, flag))
    print('\n检查 %d 组配色，不达标 %d 组（阈值 %.1f:1）' % (total, bad, THRESHOLD))
    return 0 if bad == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
