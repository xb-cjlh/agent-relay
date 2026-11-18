'use strict';
// Only file bytes cross this boundary; credentials and URLs stay in the parent.
window.addEventListener('message',function receive(event) {
  if(event.source!==parent||event.data?.type!=='relay-render-html'||typeof event.data.html!=='string')return;
  window.removeEventListener('message',receive);
  // The response's CSP and the iframe sandbox remain in force after document.write.
  document.open();document.write(event.data.html);document.close();
});
