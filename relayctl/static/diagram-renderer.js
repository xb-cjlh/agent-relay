'use strict';

// Both the shared engine and lightweight display documents have opaque origins.
// Only the engine imports Mermaid, whose diagram implementations load on demand.
let library, fullLibrary = false, rendering = false, displayed = false;
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
function displayDiagram(svg) {
  const container = document.getElementById('diagram'); container.innerHTML = svg;
  for (const link of container.querySelectorAll('a')) {
    link.removeAttribute('href'); link.removeAttribute('xlink:href'); link.removeAttribute('target');
  }
  const drawing = container.querySelector('svg'), box = drawing.viewBox.baseVal;
  drawing.style.width = Math.ceil(box.width) + 'px'; drawing.style.height = Math.ceil(box.height) + 'px'; drawing.style.maxWidth = 'none';
  return Math.ceil(box.height)+30;
}
window.addEventListener('message',async event => {
  if (event.source !== parent) return;
  const data = event.data;
  if (data?.type === 'relay-show-mermaid' && !displayed) {
    displayed = true;
    try {
      if (typeof data.svg !== 'string' || data.svg.length > 4000000) throw new Error('Diagram size');
      parent.postMessage({type:'relay-mermaid-result',ok:true,height:displayDiagram(data.svg)},'*');
    } catch { parent.postMessage({type:'relay-mermaid-result',ok:false},'*'); }
  }
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
