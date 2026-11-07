// LaTeX in rendered markdown: a chat reply on / and /reader's chat, and a chunk's
// original text in /reader. Needs KaTeX on the page; without it math shows as source.
(function () {
  // Math is pulled out before markdown runs, or `_` and `\\` inside it get eaten as
  // emphasis and escapes. Pandoc's rule for inline $: no space just inside either
  // dollar, and no digit right after the closing one (so "$5 and $10" stays text).
  // tts.py and Android's MathDelimiters find math by the same rules.
  const MATH = [
    { re: /\$\$([\s\S]+?)\$\$/g, display: true },
    { re: /\\\[([\s\S]+?)\\\]/g, display: true },
    { re: /\\\(([\s\S]+?)\\\)/g, display: false },
    { re: /(?<![\\$])\$(?![\s$])((?:[^$\n]|\n(?!\n))*?[^\s\\$])\$(?!\d)/g, display: false },
    { re: /(?<![\\$])\$([^\s$\\])\$(?!\d)/g, display: false },
  ];
  // Fenced blocks and code spans are not math: `echo $a$b` must stay as written.
  const CODE = /^[ \t]*(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:^[ \t]*\1[ \t]*$|(?![\s\S]))|(`+)[^\n]+?\2/gm;

  function extract(source) {
    const maths = [];
    const code = [];
    let text = String(source || '').replace(CODE, (m) => {
      code.push(m);
      return `OCTCODE${code.length - 1}X`;
    });
    for (const { re, display } of MATH) {
      text = text.replace(re, (_m, tex) => {
        maths.push({ tex: tex.trim(), display });
        return `OCTMATH${maths.length - 1}X`;
      });
    }
    text = text.replace(/OCTCODE(\d+)X/g, (m, i) => (i < code.length ? code[i] : m));
    return { text, maths };
  }

  function insertSlots(html, maths) {
    return html.replace(/OCTMATH(\d+)X/g, (m, i) => (i < maths.length ? `<span class="math-slot" data-m="${Number(i)}"></span>` : m));
  }

  // katex.render, not renderToString: the string form carries style="..." attributes,
  // which the page's CSP blocks; render() sets the same styles through the CSSOM.
  function renderTex(el, tex, display) {
    try {
      if (typeof katex === 'undefined') throw new Error('KaTeX not loaded');
      katex.render(tex, el, { displayMode: display, throwOnError: false, strict: 'ignore', output: 'html' });
    } catch (_err) {
      el.textContent = tex;
      el.classList.add('math-raw');
    }
  }

  function fill(root, maths) {
    root.querySelectorAll('span.math-slot').forEach((el) => {
      const m = maths[Number(el.dataset.m)];
      if (m) renderTex(el, m.tex, m.display);
    });
  }

  // Sets el's content to a markdown reply: toHtml turns markdown into safe HTML.
  function setMarkdown(el, source, toHtml) {
    const { text, maths } = extract(source);
    el.innerHTML = insertSlots(toHtml(text), maths);
    fill(el, maths);
  }

  window.OctaviusMath = { extract, insertSlots, fill, setMarkdown };
})();
