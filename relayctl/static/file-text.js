'use strict';

function previewText(data) {
  const bytes = new Uint8Array(data), limit = 256 * 1024, slice = bytes.subarray(0,limit);
  const encoding = bytes[0] === 0xff && bytes[1] === 0xfe ? 'utf-16le' : bytes[0] === 0xfe && bytes[1] === 0xff ? 'utf-16be' : 'utf-8';
  let text;
  try { text = new TextDecoder(encoding,{fatal:true}).decode(slice,{stream:bytes.length > limit}); }
  catch { return null; }
  if (/[\u0000-\u0008\u000b\u000e-\u001f]/.test(text)) return null;
  const lines = text.replace(/\r\n?/g,'\n').split('\n');
  const truncated = bytes.length > limit || lines.length > 5000;
  return {text:lines.slice(0,5000).join('\n'),truncated};
}
