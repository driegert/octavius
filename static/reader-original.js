// Renders a reader chunk's original markdown (its `source_text`: tables, LaTeX and
// citations intact) for /reader's Original view. The text comes from converted PDFs
// and web pages, so it is untrusted: raw HTML is escaped except a few bare formatting
// tags, links survive only as http(s)/mailto, and images are never loaded.
(function () {
  const esc = OctaviusApp.escapeHtml;

  // A private instance: reader-chat.js configures the global `marked` for chat replies.
  const md = typeof marked !== 'undefined' && marked.Marked ? new marked.Marked({ gfm: true, breaks: false }) : null;
  if (md) {
    md.use({
      renderer: {
        // docproc emits things like Rate<sub>c</sub>; keep bare formatting tags, escape the rest.
        html(token) {
          const raw = typeof token === 'string' ? token : (token && (token.raw || token.text)) || '';
          return esc(raw).replace(/&lt;(\/?)(sub|sup|br|em|strong|b|i|u)\s*(\/?)&gt;/gi,
            (_m, close, tag, self) => `<${close}${tag.toLowerCase()}${self}>`);
        },
        // Figures are not served to the page; name them instead.
        image(token) {
          const label = (token && token.text) || 'figure';
          return `<span class="orig-figure">[Figure: ${esc(label)}]</span>`;
        },
      },
      walkTokens(token) {
        if (token.type === 'link' && !/^(https?:|mailto:)/i.test(token.href || '')) token.href = '';
      },
    });
  }

  // Math is pulled out before markdown runs, or `_` and `\\` inside it get eaten as
  // emphasis and escapes. Pandoc's rule for inline $: no space just inside either
  // dollar, and no digit right after the closing one (so "$5 and $10" stays text).
  const MATH = [
    { re: /\$\$([\s\S]+?)\$\$/g, display: true },
    { re: /\\\[([\s\S]+?)\\\]/g, display: true },
    { re: /\\\(([\s\S]+?)\\\)/g, display: false },
    { re: /(?<![\\$])\$(?![\s$])((?:[^$\n]|\n(?!\n))*?[^\s\\$])\$(?!\d)/g, display: false },
    { re: /(?<![\\$])\$([^\s$\\])\$(?!\d)/g, display: false },
  ];

  // katex.render, not renderToString: the string form carries style="..." attributes,
  // which the page's CSP blocks; render() sets the same styles through the CSSOM.
  function renderTex(el, tex, display) {
    try {
      if (typeof katex === 'undefined') throw new Error('KaTeX not loaded');
      katex.render(tex, el, { displayMode: display, throwOnError: false, strict: 'ignore', output: 'html' });
    } catch (_err) {
      el.textContent = tex;
      el.classList.add('orig-math-raw');
    }
  }

  // Quarto cross-reference attributes ({#eq-x}, {#tbl-y .class}) and ::: div fences
  // are markup, not text.
  function stripQuarto(text) {
    return text
      .replace(/^[ \t]*:::.*$/gm, '')
      .replace(/[ \t]*\{#[A-Za-z][\w:.-]*[^}\n]*\}/g, '');
  }

  // Returns a DocumentFragment, built in an inert <template> (nothing loads or runs).
  function render(source) {
    const maths = [];
    let text = String(source || '');
    for (const { re, display } of MATH) {
      text = text.replace(re, (_m, tex) => {
        maths.push({ tex: tex.trim(), display });
        return `OCTMATH${maths.length - 1}X`;
      });
    }
    text = stripQuarto(text);
    let html;
    try {
      html = md ? md.parse(text) : `<p>${esc(text)}</p>`;
    } catch (_err) {
      html = `<p>${esc(text)}</p>`;
    }
    html = html.replace(/OCTMATH(\d+)X/g, (_m, i) => `<span class="orig-math" data-m="${Number(i)}"></span>`);
    const tpl = document.createElement('template');
    tpl.innerHTML = html;
    tpl.content.querySelectorAll('span.orig-math').forEach((el) => {
      const m = maths[Number(el.dataset.m)];
      if (m) renderTex(el, m.tex, m.display);
    });
    tpl.content.querySelectorAll('a').forEach((a) => {
      const href = a.getAttribute('href') || '';
      if (/^(https?:|mailto:)/i.test(href)) {
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
      } else {
        a.replaceWith(...a.childNodes);
      }
    });
    tpl.content.querySelectorAll('table').forEach((t) => {
      const wrap = document.createElement('div');
      wrap.className = 'orig-table';
      t.replaceWith(wrap);
      wrap.appendChild(t);
    });
    return tpl.content;
  }

  // Whether the original reads differently from the spoken text, i.e. whether a
  // per-chunk flip is worth offering. Math, tables, HTML and images always do.
  function differs(source, sentences) {
    const src = String(source || '');
    if (/[$|\\<]|!\[/.test(src)) return true;
    const norm = (s) => s.replace(/[*_`#>]/g, '').replace(/\s+/g, ' ').trim();
    return norm(src) !== norm((sentences || []).join(' '));
  }

  // Which spoken sentence a click at `offset` in the original's text most likely means.
  // There is no sentence map (math and tables are rewritten for speech), so: find the
  // words around the click in the sentences, longest run first, preferring the match
  // nearest the click's relative position; where nothing matches (inside math), use
  // that relative position alone. Blank sentences are never chosen.
  function sentenceAt(full, offset, sentences) {
    const norm = (t) => String(t || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
    const sents = (sentences || []).map(norm);
    const usable = sents.map((t, i) => (t ? i : -1)).filter((i) => i >= 0);
    if (!usable.length) return 0;
    full = String(full || '');
    offset = Math.max(0, Math.min(full.length, offset || 0));

    // Proportional guess, by spoken length.
    const lens = sents.map((t) => t.length + 1);
    const total = lens.reduce((x, y) => x + y, 0);
    const target = (full.length ? offset / full.length : 0) * total;
    let guess = usable[usable.length - 1];
    for (let i = 0, acc = 0; i < lens.length; i++) {
      acc += lens[i];
      if (acc > target && sents[i]) { guess = i; break; }
    }

    const before = norm(full.slice(0, offset)).split(' ').filter(Boolean);
    const after = norm(full.slice(offset)).split(' ').filter(Boolean);
    // The clicked word may be split by the offset: join its halves.
    const partial = /[\p{L}\p{N}]$/u.test(full.slice(0, offset)) && /^[\p{L}\p{N}]/u.test(full.slice(offset));
    const head = partial && before.length ? before.pop() + (after.shift() || '') : (after.shift() || before.pop() || '');
    if (!head) return guess;
    for (const [nb, na] of [[2, 3], [1, 2], [0, 2], [1, 1], [0, 1], [0, 0]]) {
      const words = [...before.slice(before.length - nb), head, ...after.slice(0, na)];
      if (nb > before.length || na > after.length) continue;
      if (words.join('').length < 4) continue;  // "a", "of": too common to mean anything
      const phrase = ' ' + words.join(' ') + ' ';
      const hits = usable.filter((i) => (' ' + sents[i] + ' ').includes(phrase));
      if (hits.length) return hits.reduce((x, y) => (Math.abs(y - guess) < Math.abs(x - guess) ? y : x));
    }
    return guess;
  }

  window.OctaviusReaderOriginal = { render, differs, sentenceAt };
})();
