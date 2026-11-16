# -*- coding: utf-8 -*-
"""核对编辑器里的正文：完整性、重复、以及每块的实际内联配色。"""
import io
import json
import re
import sys

sys.path.insert(0, 'tools')
import paste_html as P  # noqa: E402

DUMP_JS = r"""(function(){
  var b = document.querySelector('#ueditor_0 .ProseMirror');
  if (!b) return null;
  var out = [];
  Array.prototype.slice.call(b.children).forEach(function(el, i){
    var st = el.getAttribute('style') || '';
    out.push({i: i + 1, tag: el.tagName.toLowerCase(),
              color: (st.match(/color:\s*([^;]+)/) || [])[1] || '',
              bg: (st.match(/background(?:-color)?:\s*([^;]+)/) || [])[1] || '',
              text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 40),
              spans: Array.prototype.slice.call(el.querySelectorAll('span')).map(function(s){
                return (s.getAttribute('style') || '').match(/color:\s*([^;]+)/)
                  ? (s.getAttribute('style') || '') : '';
              }).filter(Boolean).length});
  });
  return {blocks: out, html: b.innerHTML, title:
    (document.querySelector('.title-editor__input .ProseMirror') || {}).innerText};
})()"""


def main():
    d = P.ev(DUMP_JS)
    if not d:
        print('❌ 编辑器不在页面上')
        return 1
    blocks = d['blocks']
    print('标题：', d['title'])
    print('块数：', len(blocks))
    for b in blocks:
        print('%2d %-10s color=%-10s bg=%-10s spans=%d %s'
              % (b['i'], b['tag'], b['color'] or '-', b['bg'] or '-', b['spans'], b['text']))

    io.open('C:/temp/live-body.html', 'w', encoding='utf-8').write(d['html'])

    # 与源文对比
    src = io.open('materials/ai-compute-1115/body.md', encoding='utf-8').read()
    paras = [re.sub(r'^#+\s*', '', p.strip()) for p in re.split(r'\n\s*\n', src) if p.strip()]
    paras = [p for p in paras if p != '---' and not p.startswith('资料来源')]
    flat = re.sub(r'[\s\u3000]', '', ''.join(b['text'] for b in blocks))
    page_all = re.sub(r'[\s\u3000]', '', P.ev(
        r"""(function(){var b=document.querySelector('#ueditor_0 .ProseMirror');return b.innerText;})()"""))
    miss = [p[:24] for p in paras if re.sub(r'[\s\u3000]', '', p)[:20] not in page_all]
    print('\n源段落 %d ｜ 未落到页面 %d' % (len(paras), len(miss)))
    for m in miss[:6]:
        print('  miss:', m)
    seen, dup = set(), []
    for b in blocks:
        k = b['text'][:20]
        if len(b['text']) > 15:
            if k in seen:
                dup.append(k)
            seen.add(k)
    print('重复块 %d' % len(dup))
    for x in dup[:6]:
        print('  dup:', x)
    return 0


if __name__ == '__main__':
    sys.exit(main())
