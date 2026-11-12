'use strict';

// Only complete Mermaid fences render; partial streamed output remains readable code.
function diagramFence(token, env) {
  const last = env.source?.split('\n')[token.map[1] - 1]?.trim() || '';
  if (!new RegExp('^' + token.markup[0] + '{' + token.markup.length + ',}\\s*$').test(last)) return null;
  return `<div class="code-block mermaid-block" data-mermaid><div class="code-heading"><span>mermaid</span><button type="button" data-copy-code>复制源码</button></div><div class="mermaid-view" aria-label="Mermaid 图表"><p class="mermaid-status" role="status">正在绘制图表…</p></div><details class="mermaid-source"><summary>查看源码</summary><pre><code>${esc(token.content)}</code></pre></details></div>`;
}

let diagramEngine, diagramEngineReady, diagramActive, diagramSequence = 0, diagramCacheBytes = 0;
const diagramQueue = [], diagramPending = new Map(), diagramCache = new Map();
const diagramObserver = new IntersectionObserver(entries => {
  for (const entry of entries) if (entry.isIntersecting) {
    diagramObserver.unobserve(entry.target); requestDiagram(entry.target);
  }
},{rootMargin:'300px'});

function diagramFailed(block) {
  if (!block.isConnected) return;
  clearTimeout(block.querySelector('iframe')?.diagramTimer);
  block.querySelector('.mermaid-view').textContent = '无法绘制图表，已显示源码。';
  block.querySelector('details').open = true;
}

function clearDiagrams() {
  diagramObserver.disconnect();
  clearTimeout(diagramActive?.timer);
  for (const frame of document.querySelectorAll('iframe[data-mermaid-frame]')) clearTimeout(frame.diagramTimer);
  diagramEngine?.remove(); diagramEngine = diagramEngineReady = diagramActive = null;
  diagramQueue.length = 0; diagramPending.clear(); diagramCache.clear(); diagramCacheBytes = 0;
}

function engineReady() {
  if (!diagramEngineReady) diagramEngineReady = new Promise((resolve,reject) => {
    const frame = document.createElement('iframe');
    frame.className = 'mermaid-engine'; frame.title = '图表渲染引擎';
    frame.setAttribute('sandbox','allow-scripts'); frame.setAttribute('aria-hidden','true'); frame.tabIndex = -1;
    frame.setAttribute('referrerpolicy','no-referrer');
    frame.onload = () => resolve(frame);
    frame.onerror = () => reject(new Error('Diagram engine unavailable'));
    frame.src = '/diagram.html'; diagramEngine = frame; document.body.append(frame);
  });
  return diagramEngineReady;
}

function rememberDiagram(source,result) {
  const size = source.length + (result.svg?.length || 0);
  if (size > 4000000) return;
  diagramCache.set(source,{result,size}); diagramCacheBytes += size;
  while (diagramCache.size > 80 || diagramCacheBytes > 4000000) {
    const key = diagramCache.keys().next().value;
    diagramCacheBytes -= diagramCache.get(key).size; diagramCache.delete(key);
  }
}

function showDiagram(block,result) {
  if (!block.isConnected) return;
  if (!result.ok) { diagramFailed(block); return; }
  const frame = document.createElement('iframe');
  frame.title = 'Mermaid 图表'; frame.className = 'mermaid-frame';
  frame.setAttribute('sandbox','allow-scripts'); frame.setAttribute('referrerpolicy','no-referrer');
  frame.dataset.mermaidFrame = '';
  frame.addEventListener('load',() => {
    if (frame.isConnected) frame.contentWindow.postMessage({type:'relay-show-mermaid',svg:result.svg},'*');
  },{once:true});
  frame.addEventListener('error',() => diagramFailed(block),{once:true});
  frame.diagramTimer = setTimeout(() => diagramFailed(block),15000);
  frame.src = '/diagram.html'; block.querySelector('.mermaid-view').append(frame);
}

function requestDiagram(block) {
  if (!block.isConnected) return;
  const source = block.querySelector('code').textContent;
  if (source.length > 50000) { diagramFailed(block); return; }
  const cached = diagramCache.get(source);
  if (cached) {
    diagramCache.delete(source); diagramCache.set(source,cached);
    showDiagram(block,cached.result); return;
  }
  let job = diagramPending.get(source);
  if (!job) {
    job = {source,blocks:new Set(),id:++diagramSequence};
    diagramPending.set(source,job); diagramQueue.push(job);
  }
  job.blocks.add(block); runDiagramQueue();
}

async function runDiagramQueue() {
  if (diagramActive || !diagramQueue.length) return;
  const job = diagramQueue.shift();
  if (![...job.blocks].some(block => block.isConnected)) {
    diagramPending.delete(job.source); runDiagramQueue(); return;
  }
  diagramActive = job;
  // Bound library loading and rendering together, including network failures.
  job.timer = setTimeout(() => {
    if (diagramActive !== job) return;
    for (const block of job.blocks) diagramFailed(block);
    diagramPending.delete(job.source); diagramActive = null;
    diagramEngine?.remove(); diagramEngine = diagramEngineReady = null;
    runDiagramQueue();
  },30000);
  try {
    const engine = await engineReady();
    if (diagramActive === job) engine.contentWindow.postMessage({type:'relay-render-mermaid',id:job.id,source:job.source},'*');
  } catch {
    if (diagramActive === job) {
      diagramEngine?.remove(); diagramEngine = diagramEngineReady = null;
      finishDiagram({ok:false});
    }
  }
}

function finishDiagram(result) {
  const job = diagramActive;
  if (!job) return;
  clearTimeout(job.timer);
  // Only successful output is reused; transient failures can be retried on revisit.
  if (result.ok) rememberDiagram(job.source,result);
  diagramPending.delete(job.source); diagramActive = null;
  for (const block of job.blocks) showDiagram(block,result);
  runDiagramQueue();
}

function renderDiagrams(container) {
  for (const block of container.querySelectorAll('[data-mermaid]:not([data-diagram-started])')) {
    block.dataset.diagramStarted = '1'; diagramObserver.observe(block);
  }
}

// Keep completed diagrams and their browsing contexts in place when streaming
// adds text around them. Replacing an article's innerHTML restarts every iframe.
function setMessageMarkup(node,html) {
  if (!node.querySelector('[data-mermaid]')) { node.innerHTML = html; return; }
  const template = document.createElement('template'); template.innerHTML = html;
  function patch(current,next) {
    let before = current.firstChild;
    for (const child of [...next.childNodes]) {
      if (!before) { current.append(child); continue; }
      const old = before; before = old.nextSibling;
      if (old.nodeType !== child.nodeType || old.nodeName !== child.nodeName) { old.replaceWith(child); continue; }
      if (child.nodeType === Node.TEXT_NODE) { if (old.data !== child.data) old.data = child.data; continue; }
      if (child.nodeType !== Node.ELEMENT_NODE) continue;
      if (old.matches('[data-mermaid]') || child.matches('[data-mermaid]')) {
        if (!old.matches('[data-mermaid]') || !child.matches('[data-mermaid]') || old.querySelector('code').textContent !== child.querySelector('code').textContent) old.replaceWith(child);
        continue;
      }
      for (const attr of [...old.attributes]) if (!child.hasAttribute(attr.name) && !(old.tagName === 'DETAILS' && attr.name === 'open')) old.removeAttribute(attr.name);
      for (const attr of child.attributes) if (old.getAttribute(attr.name) !== attr.value) old.setAttribute(attr.name,attr.value);
      patch(old,child);
    }
    while (before) { const next = before.nextSibling; before.remove(); before = next; }
  }
  patch(node,template.content);
}

window.addEventListener('message',event => {
  const data = event.data;
  if (!data) return;
  if (data.type === 'relay-mermaid-rendered' && event.source === diagramEngine?.contentWindow && data.id === diagramActive?.id) {
    finishDiagram(data); return;
  }
  if (data.type !== 'relay-mermaid-result') return;
  const frame = [...document.querySelectorAll('iframe[data-mermaid-frame]')].find(item => item.contentWindow === event.source);
  if (!frame) return;
  const block = frame.closest('[data-mermaid]');
  if (!data.ok) { diagramFailed(block); return; }
  const height = Number(data.height);
  if (!Number.isFinite(height) || height < 0) return;
  clearTimeout(frame.diagramTimer);
  frame.height = String(Math.min(800,Math.max(100,Math.ceil(height))));
  block.querySelector('.mermaid-status')?.remove(); block.dataset.diagramRendered = '1';
});
