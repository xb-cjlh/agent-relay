'use strict';

// Both the shared engine and lightweight display documents have opaque origins.
// Only the engine imports Mermaid, whose diagram implementations load on demand.
let library, fullLibrary = false, rendering = false, displayed = false;
let diagramView;
async function loadMermaid() {
  if (!library) library = import(new URL('/vendor/mermaid-12.3.0/relay-common-v1.mjs',location.href).href).then(module => module.default).catch(error => { library = null; throw error; });
  return library;
}
function configureMermaid(mermaid) { mermaid.initialize({
  startOnLoad:false, securityLevel:'strict', theme:'base', logLevel:'fatal', layout:'dagre',
  suppressErrorRendering:true, maxTextSize:50000, maxEdges:500,
  secure:['secure','securityLevel','startOnLoad','maxTextSize','maxEdges','suppressErrorRendering'],
  flowchart:{htmlLabels:false},
  themeVariables:{fontFamily:'sans-serif',fontSize:'14px',primaryColor:'#f3f6ed',primaryTextColor:'#46543b',primaryBorderColor:'#a7b995',lineColor:'#7f936c',secondaryColor:'#eef3f6',tertiaryColor:'#fbfcf8'}
}); }
function displayDiagram(svg, maxHeight) {
  const container = document.getElementById('diagram'); container.innerHTML = svg;
  for (const link of container.querySelectorAll('a')) {
    link.removeAttribute('href'); link.removeAttribute('xlink:href'); link.removeAttribute('target');
  }
  const drawing = container.querySelector('svg'), box = drawing?.viewBox.baseVal;
  if (!box || !Number.isFinite(box.width) || !Number.isFinite(box.height) || box.width <= 0 || box.height <= 0) throw new Error('Invalid diagram dimensions');
  drawing.style.width = box.width + 'px'; drawing.style.height = box.height + 'px'; drawing.style.maxWidth = 'none';
  container.style.width = box.width + 'px'; container.style.height = box.height + 'px';
  document.getElementById('diagram-viewer').hidden = false;
  diagramView = {width:box.width,height:box.height,scale:1,x:0,y:0,fitted:true,maxHeight:600,lastHeight:0,viewportWidth:0,viewportHeight:0};
  setDiagramHeightLimit(maxHeight); layoutDiagram();
}

function setDiagramHeightLimit(height) {
  if (Number.isFinite(height)) diagramView.maxHeight = Math.min(800,Math.max(180,height));
}
function fitScale() {
  const viewport = document.getElementById('diagram-viewport');
  return Math.min(1,Math.max(1,viewport.clientWidth-24)/diagramView.width,Math.max(1,viewport.clientHeight-24)/diagramView.height);
}
function paintDiagram() {
  const view = diagramView, viewport = document.getElementById('diagram-viewport');
  // Keep part of the drawing reachable, even after a long drag.
  view.x = Math.min(viewport.clientWidth-24,Math.max(24-view.width*view.scale,view.x));
  view.y = Math.min(viewport.clientHeight-24,Math.max(24-view.height*view.scale,view.y));
  document.getElementById('diagram').style.transform = `translate(${view.x}px,${view.y}px) scale(${view.scale})`;
  const minimum = Math.min(.1,fitScale()/4);
  document.getElementById('zoom-level').value = (view.scale < .1 ? (view.scale*100).toFixed(1) : Math.round(view.scale*100)) + '%';
  document.getElementById('zoom-out').disabled = view.scale <= minimum;
  document.getElementById('zoom-in').disabled = view.scale >= 8;
}
function fitDiagram() {
  if (!diagramView) return;
  const viewport = document.getElementById('diagram-viewport'), view = diagramView;
  view.fitted = true; view.scale = fitScale();
  view.x = (viewport.clientWidth-view.width*view.scale)/2;
  view.y = (viewport.clientHeight-view.height*view.scale)/2;
  paintDiagram();
}
function layoutDiagram() {
  if (!diagramView) return;
  const viewport = document.getElementById('diagram-viewport'), view = diagramView;
  const toolbarHeight = document.getElementById('diagram-toolbar').offsetHeight;
  const height = Math.ceil(Math.max(180,Math.min(view.maxHeight,view.height*Math.min(1,Math.max(1,viewport.clientWidth-24)/view.width)+24+toolbarHeight)));
  if (height !== view.lastHeight) {
    view.lastHeight = height;
    parent.postMessage({type:'relay-mermaid-result',ok:true,height},'*');
  }
  if (view.fitted) fitDiagram();
  else {
    view.x += (viewport.clientWidth-view.viewportWidth)/2;
    view.y += (viewport.clientHeight-view.viewportHeight)/2;
    paintDiagram();
  }
  view.viewportWidth = viewport.clientWidth; view.viewportHeight = viewport.clientHeight;
}
function zoomDiagram(scale, x, y) {
  if (!diagramView || !Number.isFinite(scale)) return;
  const view = diagramView, viewport = document.getElementById('diagram-viewport');
  if (!Number.isFinite(x)) x = viewport.clientWidth/2;
  if (!Number.isFinite(y)) y = viewport.clientHeight/2;
  scale = Math.min(8,Math.max(Math.min(.1,fitScale()/4),scale));
  const ratio = scale/view.scale;
  view.x = x-(x-view.x)*ratio; view.y = y-(y-view.y)*ratio;
  view.scale = scale; view.fitted = false; paintDiagram();
}
const viewport = document.getElementById('diagram-viewport'), pointers = new Map();
document.getElementById('zoom-in').addEventListener('click',() => zoomDiagram(diagramView.scale*1.25));
document.getElementById('zoom-out').addEventListener('click',() => zoomDiagram(diagramView.scale/1.25));
document.getElementById('zoom-fit').addEventListener('click',fitDiagram);
document.getElementById('zoom-actual').addEventListener('click',() => zoomDiagram(1));
viewport.addEventListener('wheel',event => {
  if (!diagramView) return;
  event.preventDefault();
  const rect = viewport.getBoundingClientRect(), delta = event.deltaY*(event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.clientHeight : 1);
  zoomDiagram(diagramView.scale*Math.exp(-Math.max(-100,Math.min(100,delta))*.008),event.clientX-rect.left,event.clientY-rect.top);
},{passive:false});
// Safari trackpads expose pinch as gesture events instead of wheel events.
let gestureScale;
viewport.addEventListener('gesturestart',event => { event.preventDefault(); gestureScale = diagramView?.scale; },{passive:false});
viewport.addEventListener('gesturechange',event => {
  event.preventDefault();
  if (!gestureScale || pointers.size >= 2 || !Number.isFinite(event.scale)) return;
  const rect = viewport.getBoundingClientRect();
  zoomDiagram(gestureScale*event.scale,event.clientX-rect.left,event.clientY-rect.top);
},{passive:false});
viewport.addEventListener('gestureend',event => { event.preventDefault(); gestureScale = null; },{passive:false});
viewport.addEventListener('dblclick',fitDiagram);
viewport.addEventListener('keydown',event => {
  if (!diagramView) return;
  if (event.key === '+' || event.key === '=') zoomDiagram(diagramView.scale*1.25);
  else if (event.key === '-') zoomDiagram(diagramView.scale/1.25);
  else if (event.key === '0') fitDiagram();
  else if (event.key === '1') zoomDiagram(1);
  else if (['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)) {
    diagramView.x += event.key === 'ArrowLeft' ? 32 : event.key === 'ArrowRight' ? -32 : 0;
    diagramView.y += event.key === 'ArrowUp' ? 32 : event.key === 'ArrowDown' ? -32 : 0;
    diagramView.fitted = false; paintDiagram();
  } else return;
  event.preventDefault();
});
viewport.addEventListener('pointerdown',event => {
  if (!diagramView || event.pointerType === 'mouse' && event.button !== 0) return;
  event.preventDefault(); viewport.focus({preventScroll:true});
  viewport.setPointerCapture(event.pointerId); pointers.set(event.pointerId,{x:event.clientX,y:event.clientY});
  viewport.classList.add('dragging');
});
viewport.addEventListener('pointermove',event => {
  if (!pointers.has(event.pointerId)) return;
  const previous = [...pointers.values()].slice(0,2);
  pointers.set(event.pointerId,{x:event.clientX,y:event.clientY});
  const next = [...pointers.values()].slice(0,2), view = diagramView;
  if (previous.length === 2) {
    const center = points => ({x:(points[0].x+points[1].x)/2,y:(points[0].y+points[1].y)/2});
    const distance = points => Math.hypot(points[1].x-points[0].x,points[1].y-points[0].y);
    const from = center(previous), to = center(next), rect = viewport.getBoundingClientRect();
    if (distance(previous) > 0) zoomDiagram(view.scale*distance(next)/distance(previous),from.x-rect.left,from.y-rect.top);
    view.x += to.x-from.x; view.y += to.y-from.y;
  } else { view.x += next[0].x-previous[0].x; view.y += next[0].y-previous[0].y; }
  view.fitted = false; paintDiagram();
});
function endPointer(event) {
  pointers.delete(event.pointerId);
  if (!pointers.size) viewport.classList.remove('dragging');
}
for (const type of ['pointerup','pointercancel','lostpointercapture']) viewport.addEventListener(type,endPointer);
new ResizeObserver(layoutDiagram).observe(viewport);

window.addEventListener('message',async event => {
  if (event.source !== parent) return;
  const data = event.data;
  if (data?.type === 'relay-show-mermaid' && !displayed) {
    displayed = true;
    try {
      if (typeof data.svg !== 'string' || data.svg.length > 4000000) throw new Error('Diagram size');
      displayDiagram(data.svg,data.maxHeight);
    } catch { parent.postMessage({type:'relay-mermaid-result',ok:false},'*'); }
  }
  if (data?.type === 'relay-resize-mermaid' && displayed && diagramView) { setDiagramHeightLimit(data.maxHeight); layoutDiagram(); }
  if (data?.type !== 'relay-render-mermaid' || rendering) return;
  rendering = true;
  try {
    if (typeof data.source !== 'string' || data.source.length > 50000) throw new Error('Diagram size');
    let mermaid = await loadMermaid(); configureMermaid(mermaid);
    // render() already parses its input. Calling parse() separately duplicates it.
    let result;
    globalThis.relayMermaidOptional = false;
    try {
      result = await mermaid.render('relay-diagram-'+data.id,data.source);
    } catch (error) {
      if (fullLibrary || !globalThis.relayMermaidOptional) throw error;
      // Uncommon diagram types, math, and explicitly requested heavy layouts
      // retain the complete upstream implementation, with one consistent config.
      library = import(new URL('/vendor/mermaid-12.3.0/mermaid.esm.min.mjs',location.href).href).then(module => module.default).catch(error => { library = null; fullLibrary = false; throw error; });
      fullLibrary = true; mermaid = await library; configureMermaid(mermaid);
      result = await mermaid.render('relay-diagram-'+data.id,data.source);
    }
    const {svg} = result;
    parent.postMessage({type:'relay-mermaid-rendered',id:data.id,ok:true,svg},'*');
  } catch {
    parent.postMessage({type:'relay-mermaid-rendered',id:data.id,ok:false},'*');
  } finally { rendering = false; document.getElementById('diagram').replaceChildren(); }
});
