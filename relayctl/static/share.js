'use strict';
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const token=location.hash.slice(1);
const standalone=/^\/file\/?$/.test(location.pathname);
const privateFile=standalone&&/^(file|att|img)_[A-Za-z0-9]+$/.test(token);
if(standalone)document.body.classList.add('standalone-file');
if(privateFile)$('share-access').textContent='文件预览';
let expiresAt=0,before=null,kind='',expired=false,expiryTimer;
let sharedFile=null,fileObjectUrl=null,previewRequest=null;
const markdown=window.markdownit({html:false,breaks:true,linkify:true}).disable('image');
markdown.renderer.rules.link_open=(tokens,index,options,env,renderer)=>{
  const href=tokens[index].attrGet('href')||'';
  if(!/^(https?:|mailto:)/i.test(href)) {
    tokens[index].attrSet('href','#unshared-file');tokens[index].attrSet('data-unshared','');
    tokens[index].attrSet('title','本地文件链接未包含在此分享中');
  }
  tokens[index].attrSet('target','_blank');tokens[index].attrSet('rel','noopener noreferrer');
  tokens[index].attrSet('referrerpolicy','no-referrer');return renderer.renderToken(tokens,index,options);
};
markdown.renderer.rules.fence=(tokens,index,options,env)=>{
  const part=tokens[index],language=part.info.trim().split(/\s+/)[0];
  if(language.toLowerCase()==='mermaid'){const diagram=diagramFence(part,env);if(diagram)return diagram;}
  return `<div class="code-block"><div class="code-heading"><span>${esc(language)}</span><button type="button" data-copy-code>复制</button></div><pre><code>${esc(part.content)}</code></pre></div>`;
};
markdown.renderer.rules.table_open=()=>'<div class="markdown-table-wrap"><table class="markdown-table">';
markdown.renderer.rules.table_close=()=>'</table></div>';
for(const tag of ['th_open','td_open'])markdown.renderer.rules[tag]=(tokens,index,options,env,renderer)=>{
  const part=tokens[index],alignment=/^text-align:(left|center|right)$/.exec(part.attrGet('style')||'');
  if(alignment){part.attrs=part.attrs.filter(([name])=>name!=='style');part.attrSet('class','markdown-align-'+alignment[1]);}
  return renderer.renderToken(tokens,index,options);
};
const date=value=>new Date(value*1000).toLocaleString('zh-CN');
const size=value=>value>=1048576?(value/1048576).toFixed(1)+' MiB':value>=1024?(value/1024).toFixed(1)+' KiB':value+' B';

function unavailable(message=privateFile?'文件不可用或已过期':'分享链接已失效或不存在') {
  expired=true;document.title='Relay · 分享';clearTimeout(expiryTimer);clearDiagrams();$('share-messages').replaceChildren();
  previewRequest?.abort();previewRequest=null;sharedFile=null;
  if(fileObjectUrl)URL.revokeObjectURL(fileObjectUrl);fileObjectUrl=null;
  $('shared-file-content').replaceChildren();$('shared-file').hidden=true;
  $('share-title').textContent='分享内容';$('share-meta').textContent='';
  $('share-status').textContent=message;$('share-status').hidden=false;$('share-older').hidden=true;$('share-retry').hidden=true;
  $('share-image-dialog').close();$('share-image-view').replaceChildren();
}
async function api(path) {
  if(expired)throw new Error(privateFile?'文件不可用':'分享链接已失效');
  const endpoint=privateFile?'/api/file-preview/'+encodeURIComponent(token):'/api/shared/'+path;
  const response=await fetch(endpoint,{credentials:privateFile?'same-origin':'omit',headers:privateFile?{}:{Authorization:'Bearer '+token},cache:'no-store',redirect:'error',referrerPolicy:'no-referrer',signal:AbortSignal.timeout(20000)});
  if(!response.ok) {
    const data=await response.json().catch(()=>({}));
    if(response.status===404||response.status===401)unavailable(privateFile&&response.status===401?'请先登录 Relay，再刷新此页面。':data.error);
    throw new Error(data.error||'暂时无法加载，请重试');
  }
  const result=await response.json();
  return privateFile&&path==='content'?{type:'file',file:result,created_at:result.created_at,expires_at:result.expires_at}:result;
}
function scheduleExpiry() {
  clearTimeout(expiryTimer);
  const remaining=expiresAt-Date.now();
  if(remaining<=0){unavailable();return;}
  expiryTimer=setTimeout(scheduleExpiry,Math.min(remaining,60000));
}
async function loadImage(img,retry=false) {
  try {
    const grant=await api('images/'+encodeURIComponent(img.dataset.image)+'/url');
    if(expired||!img.isConnected)return;
    img.onerror=()=>{if(!retry&&!expired)loadImage(img,true);};img.src=grant.url;
  } catch(error){if(img.isConnected)img.alt=error.message;}
}
function render(messages,prepend) {
  const fragment=document.createDocumentFragment();
  for(const message of messages) {
    const article=document.createElement('article');article.className='message '+message.role;
    const content=message.role==='user'?esc(message.content):markdown.render(message.content,{source:message.content});
    const images=(message.images||[]).map(img=>`<button type="button" class="icon-button" data-view-image="${esc(img.id)}" title="查看 ${esc(img.name)}"><img data-image="${esc(img.id)}" alt="${esc(img.name)}" crossorigin="anonymous" referrerpolicy="no-referrer" loading="lazy"></button>`).join('');
    const files=(message.files||[]).map(file=>`<a href="#download" class="file-attachment" data-file="${esc(file.id)}" data-name="${esc(file.name)}"><span class="attachment-name">${esc(file.name)}</span><small>${size(file.size)} · 下载</small></a>`).join('');
    article.innerHTML=`${message.role==='assistant'?`<div class="message-byline">${kind==='claude'?'Claude Code':'Codex'} · ${date(message.created_at)}</div>`:''}<div class="message-body">${content}${images?`<div class="message-images">${images}</div>`:''}${files?`<div class="message-files">${files}</div>`:''}</div>`;
    fragment.append(article);
  }
  if(prepend)$('share-messages').prepend(fragment);else $('share-messages').replaceChildren(fragment);
  for(const img of $('share-messages').querySelectorAll('img[data-image]:not([src])'))loadImage(img);
  renderDiagrams($('share-messages'));
}
async function load(older=false) {
  $('share-older').disabled=true;$('share-retry').hidden=true;
  try {
    const result=await api('content'+(older?'?before='+before:''));
    if(expired)return;
    expiresAt=(result.expires_at||0)*1000;if(expiresAt)scheduleExpiry();if(expired)return;
    if(result.type==='file') {
      sharedFile=result.file;$('share-title').textContent=sharedFile.name;document.title=sharedFile.name+' · Relay';
      $('share-meta').textContent=privateFile?'':`${date(result.created_at)} 的文件快照 · ${date(result.expires_at)} 到期`;
      $('shared-file-open').href='/file#'+token;$('shared-file-open').hidden=standalone;
      $('share-older').hidden=true;$('share-messages').replaceChildren();$('shared-file').hidden=false;
      $('shared-file-info').textContent=size(sharedFile.size);$('share-status').hidden=true;
      await previewSharedFile();return;
    }
    kind=result.kind;$('share-title').textContent=result.title;document.title=result.title+' · Relay 分享';
    $('share-meta').textContent=`${date(result.created_at)} 的会话快照 · ${date(result.expires_at)} 到期`;
    before=result.next_before;$('share-older').hidden=!result.has_more;
    render(result.messages,older);$('share-status').hidden=true;
  } catch(error) {
    if(!expired){$('share-status').textContent=error.message;$('share-status').hidden=false;$('share-retry').hidden=false;}
  } finally{$('share-older').disabled=false;}
}
async function previewSharedFile() {
  previewRequest?.abort();const request=new AbortController();previewRequest=request;
  if(fileObjectUrl)URL.revokeObjectURL(fileObjectUrl);fileObjectUrl=null;
  const file=sharedFile,container=$('shared-file-content');container.textContent='正在加载预览…';
  try {
    const grant=await api('file/url');if(expired||request!==previewRequest)return;
    const local=privateFile&&grant.storage==='local';
    const response=await fetch(local?'/api/files/'+encodeURIComponent(token):grant.url,{credentials:local?'same-origin':'omit',referrerPolicy:'no-referrer',cache:'no-store',redirect:'error',signal:request.signal});
    if(local&&response.status===401){unavailable('请先登录 Relay，再刷新此页面。');return;}
    if(!response.ok)throw new Error('文件暂不可用，请重试');
    const reader=response.body.getReader(),chunks=[];let length=0;
    while(true) {
      const {value,done}=await reader.read();if(done)break;
      length+=value.byteLength;
      if(length>file.size||length>16*1024*1024){await reader.cancel();throw new Error('文件大小校验失败');}
      chunks.push(value);
    }
    const data=new Uint8Array(length);let offset=0;
    for(const chunk of chunks){data.set(chunk,offset);offset+=chunk.byteLength;}
    const digest=[...new Uint8Array(await crypto.subtle.digest('SHA-256',data))].map(x=>x.toString(16).padStart(2,'0')).join('');
    if(length!==file.size||digest!==file.sha256)throw new Error('文件校验失败，请重试');
    if(expired||request!==previewRequest)return;
    container.replaceChildren();
    document.body.classList.toggle('html-file',/\.html?$/i.test(file.name));
    if(renderHtmlFile(container,{name:file.name,data}))return;
    const ext=file.name.split('.').pop().toLowerCase();
    const mime={png:'image/png',jpg:'image/jpeg',jpeg:'image/jpeg',gif:'image/gif',webp:'image/webp',avif:'image/avif',bmp:'image/bmp'}[ext];
    if(mime) {
      const img=document.createElement('img');img.alt=file.name;
      fileObjectUrl=URL.createObjectURL(new Blob([data],{type:mime}));img.src=fileObjectUrl;
      img.onerror=()=>{if(!expired)container.textContent='浏览器无法预览此图片，请下载原文件查看。';};
      container.append(img);return;
    }
    const source=previewText(data);
    if(!source){container.textContent='此格式暂不支持预览，可下载原文件查看。';return;}
    if(source.truncated) {
      const note=document.createElement('p');note.className='file-preview-notice';
      note.textContent='文件较长，仅预览前 256 KiB / 5000 行，下载可获取完整原文件。';container.append(note);
    }
    if(['md','markdown','mdown'].includes(ext)) {
      const article=document.createElement('article');article.className='file-markdown message assistant';
      article.innerHTML='<div class="message-body">'+markdown.render(source.text,{source:source.text})+'</div>';
      container.append(article);renderDiagrams(article);
    } else {
      const pre=document.createElement('pre');pre.className='file-source';
      pre.innerHTML=source.text.split('\n').map(line=>`<span class="file-source-line">${esc(line)||'\n'}</span>`).join('');container.append(pre);
    }
  } catch(error) {
    if(!expired&&request===previewRequest){container.textContent=error.message;$('share-retry').hidden=false;}
  }
}
function download(grant,name) {
  if(expired)return;
  const link=document.createElement('a');link.href=grant.url;link.download=name;link.rel='noopener noreferrer';link.referrerPolicy='no-referrer';document.body.append(link);link.click();link.remove();
}
$('shared-file-download').addEventListener('click',async()=>{
  const button=$('shared-file-download');button.disabled=true;
  try{download(await api('file/url'),sharedFile?.name||'download');}
  catch(error){$('share-status').textContent=error.message;$('share-status').hidden=false;}
  finally{button.disabled=false;}
});
$('share-retry').addEventListener('click',()=>load());
$('share-older').addEventListener('click',()=>load(true));
$('share-image-close').addEventListener('click',()=>$('share-image-dialog').close());
$('share-image-dialog').addEventListener('close',()=>$('share-image-view').replaceChildren());
document.addEventListener('click',async event=>{
  const target=event.target.closest('[data-file],[data-view-image],[data-unshared],[data-copy-code]');if(!target)return;
  event.preventDefault();
  if(target.hasAttribute('data-unshared')){ $('share-status').textContent='本地文件链接未包含在此分享中。';$('share-status').hidden=false;return; }
  try {
    if(target.hasAttribute('data-copy-code')){await navigator.clipboard.writeText(target.closest('.code-block').querySelector('pre').textContent);return;}
    if(target.dataset.viewImage) {
      const img=document.createElement('img');img.dataset.image=target.dataset.viewImage;img.alt=target.querySelector('img').alt;img.crossOrigin='anonymous';img.referrerPolicy='no-referrer';
      $('share-image-title').textContent=img.alt;$('share-image-view').replaceChildren(img);$('share-image-dialog').showModal();await loadImage(img);return;
    }
    const grant=await api('attachments/'+encodeURIComponent(target.dataset.file)+'/url');
    download(grant,target.dataset.name);
  } catch(error){$('share-status').textContent=error.message;$('share-status').hidden=false;}
});
document.addEventListener('visibilitychange',()=>{
  if(document.hidden||expired)return;
  if(expiresAt)scheduleExpiry();
  if(privateFile)api('file/url').catch(()=>{});
});
window.addEventListener('hashchange',()=>location.reload());
window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
if(privateFile||/^shr-relay_[A-Za-z0-9_-]{43}$/.test(token))load();else unavailable();
