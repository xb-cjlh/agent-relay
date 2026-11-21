'use strict';

// Parse math before Markdown consumes LaTeX backslashes. Render into DOM nodes
// afterwards: KaTeX sets layout styles through CSSOM, keeping our strict CSP.
function mathPlaceholder(source, display) {
  return `<span class="math-formula${display ? ' math-display' : ''}" data-math="${display ? 'display' : 'inline'}">${esc(source)}</span>`;
}

function markdownMath(md) {
  md.use(texmath, {delimiters:['brackets','dollars'], engine:window.katex});
  for (const name of ['math_inline','math_inline_double','math_block','math_block_eqno']) {
    md.renderer.rules[name] = (tokens,index) => {
      const token = tokens[index], display = name !== 'math_inline';
      const number = name === 'math_block_eqno' ? `<span class="math-number">(${esc(token.info)})</span>` : '';
      return mathPlaceholder(token.content,display) + number + (token.block ? '\n' : '');
    };
  }
  // Also allow display math immediately after prose, without a blank line.
  md.inline.ruler.before('escape','math_brackets_display',texmath.inline({
    name:'math_brackets_display',tag:'\\[',rex:/\\\[([\s\S]+?)\\\]/gy
  }));
  md.renderer.rules.math_brackets_display = (tokens,index) => mathPlaceholder(tokens[index].content,true);
}

function renderMath(container) {
  for (const element of container.querySelectorAll('[data-math]')) {
    const source = element.textContent, displayMode = element.dataset.math === 'display';
    element.removeAttribute('data-math');
    if (displayMode) { element.tabIndex = 0; element.setAttribute('aria-label','数学公式'); }
    try {
      if (source.length > 20000) throw new Error('Formula too long');
      // A fresh macro scope per formula also prevents one message from changing
      // later formulas. Untrusted TeX cannot add links, images, or arbitrary HTML.
      katex.render(source,element,{displayMode,throwOnError:true,trust:false,
        strict:'ignore',maxExpand:1000,maxSize:20,macros:{},output:'htmlAndMathml'});
    } catch {
      element.textContent = source;
      element.classList.add('math-fallback');
    }
  }
}
