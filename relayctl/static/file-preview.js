'use strict';

// Paths always refer to the source device, never the browser's filesystem.
function localFileLink(href, context = {}) {
  if (typeof href !== 'string' || !href || /[\u0000-\u001f\u007f]/.test(href)) return null;
  let value = href, fragment = '', alreadyDecoded = false;
  const hash = value.indexOf('#');
  if (hash >= 0) { fragment = value.slice(hash + 1); value = value.slice(0,hash); }
  if (!value) {
    if (!context.path) return null;
    value = context.path; alreadyDecoded = true;
  } else if (/^file:/i.test(value)) {
    // A file URI with a remote host is not a path on this device.
    const uri = /^file:(?:\/\/(?:localhost)?(?=\/))?(\/[^?]*)(?:\?.*)?$/i.exec(value);
    if (!uri) return null;
    value = uri[1];
  } else {
    if (/^[a-z][a-z\d+.-]*:/i.test(value) || value.startsWith('//')) return null;
    value = value.split('?')[0];
  }
  try { if (!alreadyDecoded) value = decodeURIComponent(value); fragment = decodeURIComponent(fragment); }
  catch { return null; }
  if (!value || /[\u0000-\u001f\u007f\\]/.test(value) || /^[a-z][a-z\d+.-]*:/i.test(value) || value.startsWith('//')) return null;
  const suffix = /:(\d+)(?::\d+)?$/.exec(value);
  let line = suffix ? Number(suffix[1]) : 0;
  if (suffix) value = value.slice(0,suffix.index);
  const lines = /^L?(\d+)(?:C\d+)?(?:-L?\d+(?:C\d+)?)?$/i.exec(fragment);
  if (lines) line = Number(lines[1]);
  if (!value.startsWith('/') && !value.startsWith('~/')) {
    if (!context.base) return context.deviceId ? null : {path:value};
    value = context.base.replace(/\/$/,'') + '/' + value;
  }
  // Normalize dot segments without interpreting percent escapes a second time.
  const home = value.startsWith('~/'), parts = [];
  for (const part of value.slice(home ? 2 : 1).split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') {
      // Let the device expand ~ before resolving parent directories.
      if (home && (!parts.length || parts.at(-1) === '..')) parts.push(part);
      else parts.pop();
    } else parts.push(part);
  }
  return {deviceId:context.deviceId,path:(home ? '~/' : '/') + parts.join('/'),line,fragment:lines ? '' : fragment};
}

async function readArtifact(id, expected) {
  const grant = await api('files/' + encodeURIComponent(id) + '/url');
  const response = grant.storage === 'oss' ? await fetch(grant.url,{credentials:'omit',referrerPolicy:'no-referrer'}) : await api('files/' + encodeURIComponent(id),undefined,true);
  if (!response.ok) throw new Error('缓存文件暂不可用，请重试');
  const data = await response.arrayBuffer(), sha = grant.sha256;
  if (data.byteLength > 16 * 1024 * 1024) throw new Error('文件超过 16 MiB 限制');
  if (!crypto.subtle) throw new Error('浏览器文件校验需要 HTTPS 或 localhost，也可使用 CLI 下载');
  const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256',data))].map(x => x.toString(16).padStart(2,'0')).join('');
  if (hash !== sha || (expected && (hash !== expected.sha256 || data.byteLength !== expected.size))) throw new Error('文件校验失败，请重新取回');
  const encodedName = /filename\*=UTF-8''([^;]+)/i.exec(response.headers.get('Content-Disposition') || '');
  const name = expected?.name || grant.name || (encodedName ? decodeURIComponent(encodedName[1]) : 'download');
  return {id,name,data,sha256:hash};
}

function saveArtifact(file) {
  const url = URL.createObjectURL(new Blob([file.data],{type:'application/octet-stream'}));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = file.name;
  document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url),1000);
}

let filePreview = null;
function showPreviewFile(preview, entry, cached = false) {
  if (!previewCurrent(preview)) return;
  preview.entry = entry; preview.file = entry.file;
  $('file-preview-name').textContent = entry.file.name;
  $('file-preview-info').textContent = size(entry.file.data.byteLength) + (cached ? ' · 已缓存' : '');
  $('file-preview-info').title = '取回时间：' + new Date(entry.fetchedAt).toLocaleString('zh-CN');
  $('download-file-preview').disabled = false;
  renderFilePreview(preview);
}

function closeFilePreview(restoreFocus = true) {
  const previous = filePreview; filePreview = null;
  if (previous?.url) URL.revokeObjectURL(previous.url);
  if (previous?.wake) previous.wake();
  clearTimeout(previous?.checkTimer);
  if (previous?.job && !terminal.has(previous.job.status)) api('jobs/' + previous.job.id + '/cancel',{}).catch(()=>{});
  $('file-preview').hidden = true;
  $('file-preview-content').replaceChildren();
  $('file-preview-name').textContent = '文件预览';
  $('file-preview-source').textContent = '';
  $('file-preview-info').textContent = '';
  $('file-preview-info').title = '';
  $('download-file-preview').disabled = true;
  updatePreviewLayout();
  if (restoreFocus && previous?.trigger?.isConnected) previous.trigger.focus({preventScroll:true});
}

function previewCurrent(preview) { return filePreview === preview && generation === preview.account && signedIn; }

async function openFilePreview(link, trigger) {
  if (!signedIn || !link.deviceId || !state.devices.some(d => d.id === link.deviceId)) return;
  if (filePreview?.deviceId === link.deviceId && filePreview.path === link.path) {
    Object.assign(filePreview,{line:link.line,fragment:link.fragment});
    if (filePreview.file) renderFilePreview(filePreview);
    return;
  }
  closeFilePreview(false);
  const preview = {...link,account:generation,trigger}; filePreview = preview;
  $('file-preview-name').textContent = link.path.split('/').pop() || link.path;
  $('file-preview-source').textContent = state.devices.find(d => d.id === link.deviceId).name + ' · ' + link.path;
  $('file-preview-source').title = link.path;
  $('retry-file-preview').hidden = true;
  $('file-preview').hidden = false;
  updatePreviewLayout();
  $('close-file-preview').focus({preventScroll:true});
  const cached = await cachedPreviewFile(preview).catch(() => null);
  if (!previewCurrent(preview)) return;
  if (cached) showPreviewFile(preview,cached,true);
  if (!cached || Date.now() - cached.checkedAt >= filePreviewCheckInterval(preview)) await fetchFilePreview(preview);
  else scheduleFilePreviewCheck(preview);
}

function updatePreviewLayout() {
  const covered = !!filePreview && matchMedia('(max-width: 760px)').matches;
  document.querySelector('.conversation').inert = covered;
  $('sidebar').inert = covered;
}
matchMedia('(max-width: 760px)').addEventListener('change',updatePreviewLayout);

async function fetchFilePreview(preview) {
  if (!previewCurrent(preview) || preview.loading) return;
  if (preview.file && !state.devices.find(d => d.id === preview.deviceId)?.online) {
    $('file-preview-info').textContent = '设备离线，显示缓存';
    scheduleFilePreviewCheck(preview); return;
  }
  preview.loading = true;
  clearTimeout(preview.checkTimer);
  $('retry-file-preview').hidden = true;
  if (!preview.file) $('file-preview-content').replaceChildren();
  try {
    $('file-preview-info').textContent = '正在请求文件…';
    const request = {kind:'file_read',device:preview.deviceId,path:preview.path};
    if (preview.file) request.cached_artifact = preview.file.id;
    let job;
    try { job = await api('jobs',request); }
    catch(error) {
      if (error.status !== 404 || !preview.file || !previewCurrent(preview)) throw error;
      delete request.cached_artifact; job = await api('jobs',request);
    }
    preview.job = job;
    if (!previewCurrent(preview)) {
      if (!terminal.has(job.status)) api('jobs/' + job.id + '/cancel',{}).catch(()=>{});
      return;
    }
    while (!terminal.has(preview.job.status)) {
      const online = state.devices.find(d => d.id === preview.deviceId)?.online;
      $('file-preview-info').textContent = preview.file ? '已缓存 · 正在检查更新…' : preview.job.status === 'queued' ? (online ? '等待设备取回…' : '设备离线，等待上线后取回…') : '正在取回文件…';
      await new Promise(resolve => {
        const timer = setTimeout(resolve,1000);
        preview.wake = () => { clearTimeout(timer); resolve(); };
      });
      preview.wake = null;
      if (!previewCurrent(preview)) return;
      preview.job = await api('jobs/' + job.id);
      if (!previewCurrent(preview)) return;
    }
    if (preview.job.status !== 'succeeded') throw new Error(preview.job.error || (preview.job.status === 'cancelled' ? '取回已取消' : '无法取回文件'));
    if (preview.file?.id === preview.job.artifact_id) {
      preview.entry.checkedAt = Date.now();
      await persistPreviewFile(preview,preview.entry);
      if (!previewCurrent(preview)) return;
      $('file-preview-info').textContent = size(preview.file.data.byteLength) + ' · 已是最新';
    } else {
      $('file-preview-info').textContent = '正在加载预览…';
      const file = await readArtifact(preview.job.artifact_id);
      if (!previewCurrent(preview)) return;
      const scroll = $('file-preview-content').scrollTop;
      showPreviewFile(preview,await cachePreviewFile(preview,file));
      if (previewCurrent(preview) && preview.entry) $('file-preview-content').scrollTop = scroll;
    }
  } catch(error) {
    if (!previewCurrent(preview)) return;
    if (preview.file) {
      $('file-preview-info').textContent = '暂时无法检查，显示缓存';
      $('file-preview-info').title = error.message;
    } else {
      $('file-preview-info').textContent = '取回失败';
      $('file-preview-content').textContent = error.message;
      $('retry-file-preview').hidden = false;
    }
    // A network error must not leave an abandoned queued read on the device.
    if (preview.job && !terminal.has(preview.job.status)) api('jobs/' + preview.job.id + '/cancel',{}).catch(()=>{});
  } finally {
    preview.loading = false;
    if (previewCurrent(preview)) scheduleFilePreviewCheck(preview);
  }
}

function filePreviewCheckInterval(preview) {
  return state.devices.find(d => d.id === preview.deviceId)?.capabilities.conditional_files ? 10000 : 60000;
}

function scheduleFilePreviewCheck(preview) {
  clearTimeout(preview.checkTimer);
  if (!previewCurrent(preview) || !preview.file) return;
  preview.checkTimer = setTimeout(() => {
    if (!document.hidden) fetchFilePreview(preview);
    else scheduleFilePreviewCheck(preview);
  },filePreviewCheckInterval(preview));
}

document.addEventListener('visibilitychange',() => {
  if (!document.hidden && filePreview?.file && Date.now() - filePreview.entry.checkedAt >= filePreviewCheckInterval(filePreview)) fetchFilePreview(filePreview);
});

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

function renderFilePreview(preview) {
  if (!previewCurrent(preview)) return;
  const container = $('file-preview-content'), file = preview.file;
  container.replaceChildren(); container.scrollTop = 0;
  if (preview.url) { URL.revokeObjectURL(preview.url); preview.url = null; }
  const ext = file.name.split('.').pop().toLowerCase();
  const mime = {png:'image/png',jpg:'image/jpeg',jpeg:'image/jpeg',gif:'image/gif',webp:'image/webp',avif:'image/avif',bmp:'image/bmp'}[ext];
  if (mime) {
    const img = document.createElement('img'); img.alt = file.name;
    preview.url = URL.createObjectURL(new Blob([file.data],{type:mime})); img.src = preview.url;
    img.onerror = () => { if (previewCurrent(preview)) container.textContent = '浏览器无法预览此图片，请下载原文件查看。'; };
    container.append(img); return;
  }
  const source = previewText(file.data);
  if (!source) { container.textContent = '此格式暂不支持预览，可下载原文件查看。'; return; }
  if (source.truncated) {
    const note = document.createElement('p'); note.className = 'file-preview-notice';
    note.textContent = '文件较长，仅预览前 256 KiB / 5000 行，下载可获取完整原文件。'; container.append(note);
  }
  if (['md','markdown','mdown'].includes(ext) && !preview.line) {
    const article = document.createElement('article'); article.className = 'file-markdown message assistant';
    article.innerHTML = '<div class="message-body">' + richText(source.text,{deviceId:preview.deviceId,base:preview.path.slice(0,preview.path.lastIndexOf('/')) || '/',path:preview.path}) + '</div>';
    container.append(article);
    if (preview.fragment) {
      const slug = value => value.toLowerCase().trim().replace(/[^\p{L}\p{N}\s_-]/gu,'').replace(/\s+/g,'-');
      const heading = [...article.querySelectorAll('h1,h2,h3,h4,h5,h6')].find(h => slug(h.textContent) === preview.fragment.toLowerCase());
      heading?.scrollIntoView({block:'start'});
    }
  } else {
    const pre = document.createElement('pre'); pre.className = 'file-source';
    pre.innerHTML = source.text.split('\n').map((line,index) => `<span class="file-source-line${index + 1 === preview.line ? ' highlighted' : ''}" data-line="${index + 1}">${esc(line) || '\n'}</span>`).join('');
    container.append(pre);
    if (preview.line) {
      const target = pre.querySelector('.highlighted');
      if (target) target.scrollIntoView({block:'center'});
      else {
        const note = document.createElement('p'); note.className = 'file-preview-notice'; note.textContent = '指定行号不在当前预览范围内。'; container.prepend(note);
      }
    }
  }
}

document.addEventListener('click',event => {
  const link = event.target.closest('[data-file-link]');
  if (!link || event.defaultPrevented) return;
  event.preventDefault();
  try { openFilePreview(JSON.parse(link.dataset.fileLink),link); }
  catch(error) { toast(error.message); }
});
$('close-file-preview').addEventListener('click',() => closeFilePreview());
$('retry-file-preview').addEventListener('click',() => { if (filePreview) fetchFilePreview(filePreview); });
$('download-file-preview').addEventListener('click',() => { if (filePreview?.file) saveArtifact(filePreview.file); });
document.addEventListener('keydown',event => {
  if (event.key === 'Escape' && filePreview && !document.querySelector('dialog[open]')) closeFilePreview();
});
