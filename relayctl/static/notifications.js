'use strict';
// The worker only displays generic alerts. Notification details always require login.
window.RelayNotifications = (() => {
  let worker, config, cursor=null, rows=[], timer, loading=false, busy=false, enabled=false;
  let pending=new URL(location.href).searchParams.get('notification')||sessionStorage.getItem('relay-notification');
  if(!/^ntf_[a-zA-Z0-9]+$/.test(pending||''))pending=null;
  if(pending){sessionStorage.setItem('relay-notification',pending);const url=new URL(location.href);url.searchParams.delete('notification');history.replaceState(null,'',url);}
  const supported=()=>isSecureContext&&'serviceWorker' in navigator&&'PushManager' in window&&'Notification' in window;
  const ios=()=>/iPhone|iPad|iPod/.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
  const standalone=()=>matchMedia('(display-mode: standalone)').matches||navigator.standalone;
  function keyBytes(value){return Uint8Array.from(atob(value.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-value.length%4)%4)),c=>c.charCodeAt(0));}
  function matching(subscription){return subscription.options.applicationServerKey&&Array.from(new Uint8Array(subscription.options.applicationServerKey)).join()===Array.from(keyBytes(config.publicKey)).join();}
  async function registration(){if(!worker)worker=navigator.serviceWorker.register('/sw.js',{scope:'/',updateViaCache:'none'}).then(()=>navigator.serviceWorker.ready);return worker;}
  function badge(count){$('notification-count').hidden=!count;$('notification-count').textContent=count>99?'99+':String(count);}
  async function settings(){
    const account=generation;
    $('enable-push').disabled=true;$('enable-push').hidden=false;$('disable-push').hidden=true;
    if(ios()&&!standalone()){$('push-status').textContent='iPhone / iPad：在 Safari 点分享 → 添加到主屏幕，再从主屏幕打开 Relay 开启通知（需 iOS 16.4 或更新版本）。';return;}
    if(!supported()){$('push-status').textContent='当前浏览器不支持系统推送，仍可在这里查看通知。';return;}
    config=await api('push/config');if(account!==generation)return;
    if(!config.configured){$('push-status').textContent='服务端尚未配置浏览器推送，仍可查看通知。';return;}
    const subscription=await (await registration()).pushManager.getSubscription();if(account!==generation)return;
    enabled=!!subscription&&Notification.permission==='granted'&&matching(subscription);
    $('push-status').textContent=enabled?'本设备已开启通知。仅在 Agent 主动发送通知时提醒你。锁屏只显示通用提醒，详情登录后查看。':Notification.permission==='denied'?'通知权限已被拒绝，请在浏览器或系统设置中允许 Relay 发送通知。':'开启后，仅在 Agent 主动发送通知时提醒你。';
    $('enable-push').hidden=enabled;$('enable-push').disabled=busy||Notification.permission==='denied';$('disable-push').hidden=!subscription;$('disable-push').disabled=busy;
  }
  function render(){
    $('notification-list').innerHTML=rows.map(n=>`<button type="button" class="notification-entry${n.read_at?'':' unread'}" data-notification="${esc(n.id)}"><strong>${esc(n.title)}</strong><span>${esc(n.body)}</span><small>${esc(date(n.created_at))}${n.read_at?'':' · 未读'}</small></button>`).join('')||'<p class="account-help">还没有通知。</p>';
    $('more-notifications').hidden=!cursor;
  }
  async function load(more=false){
    if(!signedIn||loading)return;loading=true;const account=generation;
    try{const reply=await api('notifications'+(more&&cursor?'?before='+encodeURIComponent(cursor):''));if(account!==generation)return;
      rows=more?[...rows,...reply.notifications.filter(n=>!rows.some(old=>old.id===n.id))]:reply.notifications;cursor=reply.next_cursor;badge(reply.unread);render();
    }finally{loading=false;}
  }
  async function open(id){
    if(!signedIn){pending=id;return;}const account=generation;
    try{const n=await api('notifications/'+encodeURIComponent(id));if(account!==generation)return;await api('notifications/'+encodeURIComponent(id)+'/read',{});if(account!==generation)return;
      if(n.agent_id){$('notifications-dialog').close();deviceFilter='';$('device-filter').value='';$('search').value='';activeDirectory='';sessionOrganization='time';sessionStorage.removeItem('relay-device-filter');sessionStorage.setItem('relay-session-organization','time');selectAgent(n.agent_id,true,false);resetSessions();await refresh();}
      else{if(!$('notifications-dialog').open)$('notifications-dialog').showModal();}
      await load();
    }catch(error){toast(error.message);}
  }
  $('notifications-button').addEventListener('click',()=>{toggleSidebar(false);$('notifications-dialog').showModal();Promise.all([settings(),load()]).catch(error=>toast(error.message));});
  $('more-notifications').addEventListener('click',()=>load(true).catch(error=>toast(error.message)));
  $('notification-list').addEventListener('click',e=>{const entry=e.target.closest('[data-notification]');if(entry)open(entry.dataset.notification);});
  $('enable-push').addEventListener('click',async()=>{
    if(busy||!config?.configured||!supported())return;
    busy=true;$('enable-push').disabled=true;const account=generation;
    try{
      // Permission is requested directly from this user gesture (required by Safari).
      const permission=await Notification.requestPermission();if(account!==generation)return;
      if(permission!=='granted')throw Error('请允许通知后再开启。');
      const ready=await registration();let subscription=await ready.pushManager.getSubscription();
      if(subscription&&!matching(subscription)){await subscription.unsubscribe();subscription=null;}
      if(!subscription)subscription=await ready.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:keyBytes(config.publicKey)});
      if(account!==generation){await subscription.unsubscribe();return;}
      try{await api('push/subscriptions',subscription.toJSON());}catch(error){await subscription.unsubscribe();throw error;}
      toast('已开启本设备通知');
    }catch(error){if(account===generation)toast(error.message||'无法开启通知，请重试。');}
    finally{busy=false;if(account===generation)settings().catch(error=>toast(error.message));}
  });
  async function unsubscribe(skipServer=false){
    if(!supported()||!worker)return;
    const subscription=await (await registration()).pushManager.getSubscription();if(!subscription)return;
    // Invalidating the browser endpoint still stops delivery if the server is offline.
    try{if(signedIn&&!skipServer)await api('push/subscriptions/revoke',{endpoint:subscription.endpoint});}finally{await subscription.unsubscribe();}
  }
  $('disable-push').addEventListener('click',async()=>{if(busy)return;busy=true;$('disable-push').disabled=true;try{await unsubscribe();toast('已关闭本设备通知');}catch(error){toast(error.message);}finally{busy=false;settings().catch(error=>toast(error.message));}});
  if('serviceWorker' in navigator)navigator.serviceWorker.addEventListener('message',e=>{if(e.source?.scriptURL!==location.origin+'/sw.js')return;if(e.data?.type==='relay-open-notification'&&/^ntf_[a-zA-Z0-9]+$/.test(e.data.id))open(e.data.id);});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&signedIn)load().catch(()=>{});});
  return {
    async login(){
      clearInterval(timer);const account=generation;
      if(pending){const id=pending;pending=null;sessionStorage.removeItem('relay-notification');open(id);}
      await new Promise(resolve=>setTimeout(resolve,500));if(account!==generation||!signedIn)return;
      load().catch(()=>{});timer=setInterval(()=>{if(!document.hidden)load().catch(()=>{});},30000);
      if(supported()&&(!ios()||standalone())){
        try{config=await api('push/config');if(!config.configured||account!==generation)return;
          const subscription=await (await registration()).pushManager.getSubscription();
          if(subscription&&Notification.permission==='granted'&&matching(subscription)&&account===generation)await api('push/subscriptions',subscription.toJSON());
        }catch{/* The inbox remains available if the push service is unreachable. */}
      }
    },
    async logout(localOnly=false){clearInterval(timer);rows=[];cursor=null;badge(0);render();try{await unsubscribe(localOnly);}catch{/* Session revocation also cancels pending server delivery. */}}
  };
})();
