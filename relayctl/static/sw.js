'use strict';
self.addEventListener('install',event=>event.waitUntil(self.skipWaiting()));
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
// No fetch handler: authenticated pages, API responses and files are never cached here.
self.addEventListener('push',event=>{
  let value;try{value=event.data.json();}catch{return;}
  if(!/^ntf_[a-zA-Z0-9]+$/.test(value?.id||''))return;
  event.waitUntil(self.registration.showNotification('Relay',{
    body:'你有一条新的 Agent 通知',icon:'/icons/relay-192.png',tag:value.id,data:{id:value.id}
  }));
});
self.addEventListener('notificationclick',event=>{
  event.notification.close();const id=event.notification.data?.id;
  if(!/^ntf_[a-zA-Z0-9]+$/.test(id||''))return;
  event.waitUntil((async()=>{
    const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
    const existing=windows.find(client=>new URL(client.url).origin===self.location.origin&&new URL(client.url).pathname==='/');
    if(existing){await existing.focus();existing.postMessage({type:'relay-open-notification',id});}
    else await self.clients.openWindow('/?notification='+encodeURIComponent(id));
  })());
});
