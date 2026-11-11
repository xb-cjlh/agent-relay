'use strict';

// This document runs with an opaque origin and has no network or parent-page access.
function configureMermaid() { mermaid.initialize({
  startOnLoad:false, securityLevel:'strict', theme:'base', logLevel:'fatal',
  suppressErrorRendering:true, maxTextSize:50000, maxEdges:500,
  secure:['secure','securityLevel','startOnLoad','maxTextSize','maxEdges','suppressErrorRendering'],
  flowchart:{htmlLabels:false},
  themeVariables:{fontFamily:'sans-serif',fontSize:'14px',primaryColor:'#f3f6ed',primaryTextColor:'#46543b',primaryBorderColor:'#a7b995',lineColor:'#7f936c',secondaryColor:'#eef3f6',tertiaryColor:'#fbfcf8'}
}); }
let diagramRequested = false;
window.addEventListener('message',async event => {
  if (event.source !== parent || event.data?.type !== 'relay-render-mermaid' || diagramRequested) return;
  diagramRequested = true;
  const source = event.data.source, container = document.getElementById('diagram');
  try {
    if (typeof source !== 'string' || source.length > 50000) throw new Error('Diagram size');
    configureMermaid();
    await mermaid.parse(source);
    const {svg} = await mermaid.render('relay-diagram',source);
    container.innerHTML = svg;
    // Diagram links never navigate the frame or call functions in the host page.
    for (const link of container.querySelectorAll('a')) {
      link.removeAttribute('href'); link.removeAttribute('xlink:href'); link.removeAttribute('target');
    }
    const drawing = container.querySelector('svg'), box = drawing.viewBox.baseVal;
    drawing.style.width = Math.ceil(box.width) + 'px';
    drawing.style.height = Math.ceil(box.height) + 'px';
    drawing.style.maxWidth = 'none';
    parent.postMessage({type:'relay-mermaid-result',ok:true,height:Math.ceil(box.height)+30},'*');
  } catch {
    container.replaceChildren();
    parent.postMessage({type:'relay-mermaid-result',ok:false},'*');
  }
});
