import io
import re

s = io.open('out/ai-compute-1115/fragment.html', encoding='utf-8').read()
pat = re.compile(r'<(p|h1|h2|h3|section|blockquote|ul|ol|table|figure)\b.*?</\1>', re.S)
items = list(pat.finditer(s))
print('blocks', len(items))
for i, m in enumerate(items, 1):
    t = m.group(0)
    col = re.findall(r'color:\s*([^;">]+)', t)
    bg = re.findall(r'background(?:-color)?:\s*([^;">]+)', t)
    txt = re.sub(r'<[^>]+>', '', t)
    txt = re.sub(r'\s+', ' ', txt).strip()
    tag = re.match(r'<(\w+)', t).group(1)
    print('%2d %-10s color=%-24s bg=%-12s %s' % (
        i, tag, ','.join(col[:2]), ','.join(bg[:2]), txt[:44]))
