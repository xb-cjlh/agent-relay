'use strict';
let authMode='login', codeDeadline=0;
function setAuthMode(mode) {
  authMode=mode;
  $('auth-title').textContent={login:'登录 Relay',register:'注册 Relay',reset:'重置密码'}[mode];
  $('auth-submit').textContent={login:'登录',register:'注册',reset:'更新密码'}[mode];
  $('auth-name-field').hidden=mode!=='register';
  $('auth-code-field').hidden=mode==='login';
  $('auth-code').required=mode!=='login';
  $('auth-password').autocomplete=mode==='login'?'current-password':'new-password';
  $('auth-password').minLength=mode==='login'?0:8;
  $('auth-error').textContent='';$('auth-notice').textContent='';
  document.querySelectorAll('[data-auth-mode]').forEach(el=>{el.hidden=el.dataset.authMode===mode||(mode==='login'&&el.dataset.authMode==='login');});
}
async function signedInAs(reply) {
  signedIn=true;currentUser=reply.user;generation++;
  $('auth-password').value='';$('auth-code').value='';
  showScreen('session-loading');
  await refresh();
}
document.querySelectorAll('[data-auth-mode]').forEach(el=>el.addEventListener('click',()=>setAuthMode(el.dataset.authMode)));
$('auth-form').addEventListener('submit',async e=>{
  e.preventDefault();const button=$('auth-submit');button.disabled=true;$('auth-error').textContent='';
  try {
    const body={email:$('auth-email').value.trim(),password:$('auth-password').value,code:$('auth-code').value};
    if($('auth-name').value.trim())body.displayName=$('auth-name').value.trim();
    const reply=await api('auth/'+({reset:'reset-password'}[authMode]||authMode),body);
    if(authMode==='reset'){setAuthMode('login');$('auth-notice').textContent=reply.message;$('auth-password').value='';}
    else await signedInAs(reply);
  }catch(error){$('auth-error').textContent=error.message;}
  finally{button.disabled=false;}
});
$('send-code').addEventListener('click',async()=>{
  if(!$('auth-email').reportValidity()||Date.now()<codeDeadline)return;
  $('send-code').disabled=true;$('auth-error').textContent='';
  try{
    const reply=await api('auth/email-code',{email:$('auth-email').value.trim(),purpose:authMode==='register'?'register':'reset'});
    codeDeadline=Date.now()+reply.retryAfter*1000;$('auth-notice').textContent=reply.message;
  }catch(error){$('auth-error').textContent=error.message;}
  finally{updateCodeButton();}
});
function updateCodeButton(){const seconds=Math.max(0,Math.ceil((codeDeadline-Date.now())/1000));$('send-code').disabled=seconds>0;$('send-code').textContent=seconds?`${seconds} 秒后重发`:'获取验证码';}
setInterval(updateCodeButton,1000);
async function loadKeys(){
  const {keys}=await api('agent/keys');
  $('key-list').innerHTML=keys.map(key=>{
    const inactive=key.revoked_at||key.expires_at*1000<=Date.now();
    return `<div class="key-entry"><div>${esc(key.name)}<small>${esc(key.prefix)}… · ${key.revoked_at?'已撤销':key.expires_at*1000<=Date.now()?'已过期':'到期 '+date(key.expires_at)}${key.last_used_at?' · 最近使用 '+date(key.last_used_at):''}</small></div>${inactive?'':`<button type="button" class="button secondary" data-revoke-key="${esc(key.id)}">撤销</button>`}</div>`;
  }).join('')||'<p class="account-help">还没有 SK。创建后可用于 CLI 和设备安装。</p>';
}
$('account-button').addEventListener('click',async()=>{
  toggleSidebar(false);$('new-key-secret').textContent='';$('new-key').hidden=true;
  $('account-user').textContent=`${currentUser?.display_name||''} · ${currentUser?.email||''}`;
  $('account-dialog').showModal();
  try{await loadKeys();}catch(error){toast(error.message);}
});
$('account-dialog').addEventListener('close',()=>{$('new-key-secret').textContent='';$('new-key').hidden=true;});
$('key-form').addEventListener('submit',e=>{
  e.preventDefault();submit(e.currentTarget,async()=>{
    const key=await api('agent/keys',{name:$('key-name').value.trim(),expiresInDays:Number($('key-days').value)});
    $('new-key-secret').textContent=key.secret;$('new-key').hidden=false;$('key-name').value='';await loadKeys();
  });
});
$('copy-key').addEventListener('click',async()=>{try{await navigator.clipboard.writeText($('new-key-secret').textContent);toast('SK 已复制');}catch{toast('请选中密钥手动复制');}});
$('key-list').addEventListener('click',async e=>{
  const button=e.target.closest('[data-revoke-key]');if(!button)return;button.disabled=true;
  try{await api(`agent/keys/${button.dataset.revokeKey}/revoke`,{});await loadKeys();}catch(error){toast(error.message);button.disabled=false;}
});
let checkingSession=false;
async function checkSession(){
  if(checkingSession)return;
  checkingSession=true;
  showScreen('session-loading');
  $('session-loading-message').textContent='正在加载…';$('retry-session').hidden=true;
  try{
    const reply=await api('auth/session');
    if(reply.authenticated)await signedInAs(reply);
    else if(signedIn)await logout(true);
    else showScreen('login');
  }catch(error){
    if(error.status===401)await logout(true);
    else loadingError();
  }finally{checkingSession=false;}
}
$('retry-session').addEventListener('click',checkSession);
checkSession();
