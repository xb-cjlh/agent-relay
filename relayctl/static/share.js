'use strict';
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const token=location.hash.slice(1);
let expiresAt=0,before=null,kind='',expired=false,expiryTimer;
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

function unavailable(message='分享链接已失效或不存在') {
  expired=true;document.title='Relay · 分享会话';clearTimeout(expiryTimer);clearDiagrams();$('share-messages').replaceChildren();
  $('share-title').textContent='分享会话';$('share-meta').textContent='';
  $('share-status').textContent=message;$('share-status').hidden=false;$('share-older').hidden=true;$('share-retry').hidden=true;
  $('share-image-dialog').close();$('share-image-view').replaceChildren();
}
async function api(path) {
  if(expired)throw new Error('分享链接已失效');
  const response=await fetch('/api/shared/'+path,{credentials:'omit',headers:{Authorization:'Bearer '+token},cache:'no-store',redirect:'error',referrerPolicy:'no-referrer',signal:AbortSignal.timeout(20000)});
  if(!response.ok) {
    const data=await response.json().catch(()=>({}));
    if(response.status===404||response.status===401)unavailable(data.error);
    throw new Error(data.error||'暂时无法加载，请重试');
  }
  return response.json();
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
    const result=await api('conversation'+(older?'?before='+before:''));
    if(expired)return;
    expiresAt=result.expires_at*1000;scheduleExpiry();if(expired)return;
    kind=result.kind;$('share-title').textContent=result.title;document.title=result.title+' · Relay 分享';
    $('share-meta').textContent=`${date(result.created_at)} 的会话快照 · ${date(result.expires_at)} 到期`;
    before=result.next_before;$('share-older').hidden=!result.has_more;
    render(result.messages,older);$('share-status').hidden=true;
  } catch(error) {
    if(!expired){$('share-status').textContent=error.message;$('share-status').hidden=false;$('share-retry').hidden=false;}
  } finally{$('share-older').disabled=false;}
}
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
    if(expired)return;
    const link=document.createElement('a');link.href=grant.url;link.download=target.dataset.name;link.rel='noopener noreferrer';link.referrerPolicy='no-referrer';document.body.append(link);link.click();link.remove();
  } catch(error){$('share-status').textContent=error.message;$('share-status').hidden=false;}
});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&expiresAt)scheduleExpiry();});
window.addEventListener('hashchange',()=>location.reload());
window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
if(/^shr-relay_[A-Za-z0-9_-]{43}$/.test(token))load();else unavailable();
