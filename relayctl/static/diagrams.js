'use strict';

// Only complete Mermaid fences render; partial streamed output remains readable code.
function diagramFence(token, env) {
  const last = env.source?.split('\n')[token.map[1] - 1]?.trim() || '';
  if (!new RegExp('^' + token.markup[0] + '{' + token.markup.length + ',}\\s*$').test(last)) return null;
  return `<div class="code-block mermaid-block" data-mermaid><div class="code-heading"><span>mermaid</span><button type="button" data-copy-code>复制源码</button></div><div class="mermaid-view" aria-label="Mermaid 图表"><p class="mermaid-status" role="status">正在绘制图表…</p></div><details class="mermaid-source"><summary>查看源码</summary><pre><code>${esc(token.content)}</code></pre></details></div>`;
}

function diagramFailed(block) {
  if (!block.isConnected) return;
  clearTimeout(block.querySelector('iframe')?.diagramTimer);
  block.querySelector('.mermaid-view').textContent = '无法绘制图表，已显示源码。';
  block.querySelector('details').open = true;
}

function renderDiagrams(container) {
  for (const block of container.querySelectorAll('[data-mermaid]:not([data-diagram-started])')) {
    block.dataset.diagramStarted = '1';
    const source = block.querySelector('code').textContent;
    if (source.length > 50000) { diagramFailed(block); continue; }
    const frame = document.createElement('iframe');
    frame.title = 'Mermaid 图表'; frame.className = 'mermaid-frame';
    frame.setAttribute('sandbox','allow-scripts');
    frame.setAttribute('referrerpolicy','no-referrer');
    frame.setAttribute('loading','lazy');
    frame.dataset.mermaidFrame = '';
    frame.addEventListener('load',() => {
      if (!frame.isConnected) return;
      frame.diagramTimer = setTimeout(() => diagramFailed(block),15000);
      frame.contentWindow.postMessage({type:'relay-render-mermaid',source},'*');
    },{once:true});
    frame.addEventListener('error',() => diagramFailed(block),{once:true});
    frame.src = '/diagram.html';
    block.querySelector('.mermaid-view').append(frame);
  }
}

window.addEventListener('message',event => {
  const data = event.data;
  if (!data || data.type !== 'relay-mermaid-result') return;
  const frame = [...document.querySelectorAll('iframe[data-mermaid-frame]')].find(item => item.contentWindow === event.source);
  if (!frame) return;
  clearTimeout(frame.diagramTimer);
  const block = frame.closest('[data-mermaid]');
  if (!data.ok) { diagramFailed(block); return; }
  const height = Number(data.height);
  if (!Number.isFinite(height) || height < 0) return;
  frame.height = String(Math.min(800,Math.max(100,Math.ceil(height))));
  block.querySelector('.mermaid-status')?.remove();
  block.dataset.diagramRendered = '1';
});
