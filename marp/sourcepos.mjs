// markdown-it plugin: add data-sourcepos="L1:C1-L2:C2" (1-based, CommonMark style)
// to every rendered block token that has a token.map. Works with markdown-it 14 and 15
// and with Marp/Marpit (which is markdown-it underneath).
//
// Options:
//   attr       attribute name (default "data-sourcepos")
//   slideAttr  when Marpit slide tokens are present, attribute for the whole-slide
//              line range on <section> (default "data-slide-sourcepos"); also sets
//              data-slide-index (0-based)
export default function sourcepos(md, opts = {}) {
  const attr = opts.attr || 'data-sourcepos';
  const slideAttr = opts.slideAttr || 'data-slide-sourcepos';

  md.core.ruler.push('sourcepos', (state) => {
    if (state.inlineMode) return;
    const lines = state.src.split('\n');
    const pos = (start, endExcl) => {
      let endIdx = Math.max(start, endExcl - 1);
      // markdown-it maps for lists/list items include trailing blank lines; trim them
      while (endIdx > start && !(lines[endIdx] ?? '').trim()) endIdx--;
      const s = lines[start] ?? '';
      const e = (lines[endIdx] ?? '').replace(/\s+$/, '');
      const startCol = s.length - s.trimStart().length + 1;
      return `${start + 1}:${startCol}-${endIdx + 1}:${Math.max(e.length, 1)}`;
    };

    // Marpit: compute per-slide ranges [start, end) from slide-open tokens.
    const slides = state.tokens.filter((t) => t.type === 'marpit_slide_open');
    slides.forEach((t, i) => {
      // first slide starts at line 0 (includes front matter); others at their `---`
      const start = i === 0 ? 0 : t.map[0];
      const next = slides[i + 1];
      const end = next ? next.map[0] : lines.length;
      t.attrSet(slideAttr, pos(start, end));
      t.attrSet('data-slide-index', String(i)); // 0-based
    });

    let rowMap = null;
    for (const t of state.tokens) {
      if (t.type === 'tr_open') rowMap = t.map;
      // th/td have no map in markdown-it; inherit the row's line (cell = row line)
      if ((t.type === 'th_open' || t.type === 'td_open') && !t.map && rowMap) {
        t.attrSet(attr, pos(rowMap[0], rowMap[1]));
        continue;
      }
      if (!t.map || t.nesting < 0 || t.type === 'marpit_slide_open') continue;
      if (t.hidden) continue; // e.g. <p> inside tight list items isn't rendered
      t.attrSet(attr, pos(t.map[0], t.map[1]));
    }
  });
}
