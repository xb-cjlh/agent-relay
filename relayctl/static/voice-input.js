'use strict';
// Browser speech services handle audio. Relay receives text only when the user sends it.
window.RelayVoice = (() => {
  const Recognition=window.SpeechRecognition||window.webkitSpeechRecognition;
  const supported=!!Recognition&&isSecureContext;
  const button=$('voice-input'), prompt=$('prompt');
  let current=null;
  const unavailable='当前浏览器不支持语音识别，可使用手机键盘上的麦克风听写。';
  const errors={
    'not-allowed':'未获得麦克风或语音识别权限，请在浏览器或系统设置中允许后重试。',
    'service-not-allowed':'浏览器的语音识别服务不可用，可使用系统键盘听写。',
    'audio-capture':'无法使用麦克风，请检查设备连接或其他应用是否正在占用。',
    'network':'语音识别服务连接失败，请检查网络，或使用系统键盘听写。',
    'no-speech':'没有识别到语音，请靠近麦克风再试。',
    'language-not-supported':'当前语音服务不支持中文，可使用系统键盘听写。',
    'aborted':'语音输入已停止，已识别的文字已保留。'
  };
  function status(message='',preview=''){
    $('voice-status').hidden=!message&&!preview;
    $('voice-message').textContent=message;$('voice-preview').textContent=preview;
  }
  function valid(capture){return current===capture&&signedIn&&generation===capture.account&&selected===capture.agent&&!prompt.disabled;}
  function dispose(capture,abort=false){
    clearTimeout(capture.timer);
    capture.recognition.onstart=capture.recognition.onresult=capture.recognition.onerror=capture.recognition.onend=null;
    if(current===capture)current=null;
    if(abort)try{capture.recognition.abort();}catch{/* Already stopped. */}
  }
  function cancel(){
    if(current)dispose(current,true);
    status();render();
  }
  function render(){
    if(current&&(!valid(current)||sending))cancel();
    const active=!!current;
    button.disabled=!signedIn||!selected||prompt.disabled||sending||current?.phase==='stopping';
    button.setAttribute('aria-pressed',String(active));
    const label=active?current.phase==='stopping'?'正在结束语音输入':'停止语音输入':'语音输入';
    button.setAttribute('aria-label',label);
    button.title=active?label:supported?'语音输入（优先中文，由浏览器语音服务处理，识别后可编辑）':unavailable;
    button.innerHTML=icon(active?'stop':'microphone');
  }
  function insert(capture,text){
    if(!valid(capture))return;
    text=text.trim();if(!text)return;
    const start=prompt.selectionStart,end=prompt.selectionEnd,before=prompt.value.slice(0,start),after=prompt.value.slice(end);
    const prefix=/[a-zA-Z0-9]$/.test(before)&&/^[a-zA-Z0-9]/.test(text)?' ':'';
    const suffix=/[a-zA-Z0-9]$/.test(text)&&/^[a-zA-Z0-9]/.test(after)?' ':'';
    const room=prompt.maxLength-before.length-after.length;
    let addition=prefix+text+suffix;
    if(addition.length>room){
      addition=addition.slice(0,Math.max(0,room));
      // Do not split a surrogate pair at the input's UTF-16 maxlength boundary.
      if(/[\uD800-\uDBFF]$/.test(addition))addition=addition.slice(0,-1);
      capture.truncated=true;
    }
    if(addition){prompt.setRangeText(addition,start,end,'end');capture.received=true;prompt.dispatchEvent(new Event('input',{bubbles:true}));}
    if(capture.truncated){status('输入已达到长度上限，请编辑后再继续。');stop();}
  }
  function stop(){
    const capture=current;if(!capture||capture.phase==='stopping')return;
    capture.phase='stopping';status(capture.truncated?'输入已达到长度上限，请编辑后再继续。':'正在结束识别…');renderHeader();
    // stop() may emit a final result; keep the session bound until onend.
    try{capture.recognition.stop();}
    catch{dispose(capture,true);status('语音输入已结束，已识别文字可编辑后发送。');renderHeader();return;}
    if(current===capture)capture.timer=setTimeout(()=>{
      if(current!==capture)return;
      dispose(capture,true);status('语音输入已结束，已识别文字可编辑后发送。');renderHeader();
    },5000);
  }
  button.addEventListener('click',()=>{
    if(current){stop();return;}
    if(!signedIn||!selected||prompt.disabled||sending)return;
    if(!supported){status(unavailable);return;}
    if(prompt.value.length>=prompt.maxLength){status('输入已达到长度上限，请编辑后再继续。');return;}
    let recognition;
    try{recognition=new Recognition();}
    catch{status('无法启动浏览器语音服务，可使用系统键盘听写。');return;}
    const capture={recognition,account:generation,agent:selected,phase:'starting',finals:new Set(),received:false,timer:null};
    current=capture;recognition.lang='zh-CN';recognition.continuous=true;recognition.interimResults=true;recognition.maxAlternatives=1;
    recognition.onstart=()=>{
      if(!valid(capture)){if(current===capture)cancel();return;}
      if(capture.phase!=='stopping'){capture.phase='listening';status('正在聆听，点击麦克风停止。');render();}
    };
    recognition.onresult=event=>{
      if(!valid(capture)){if(current===capture)cancel();return;}
      let interim='';
      // Results are cumulative; revised interim text replaces the preview, finals insert once.
      for(let i=0;i<event.results.length;i++){
        const result=event.results[i],text=result[0]?.transcript||'';
        if(result.isFinal){if(!capture.finals.has(i)){capture.finals.add(i);insert(capture,text);}}
        else interim+=text;
      }
      if(current===capture&&!capture.truncated)$('voice-preview').textContent=interim;
    };
    recognition.onerror=event=>{
      if(current!==capture)return;
      dispose(capture,true);status(errors[event.error]||'语音识别暂不可用，请重试或使用系统键盘听写。');renderHeader();
    };
    recognition.onend=()=>{
      if(current!==capture)return;
      dispose(capture);
      status(capture.truncated?'输入已达到长度上限，请编辑后再继续。':capture.received?'语音输入已结束，文字可编辑后发送。':'没有识别到语音，请再试一次。');renderHeader();
    };
    status('等待语音服务和麦克风授权…');renderHeader();
    try{recognition.start();}
    catch{dispose(capture,true);status('无法启动语音识别，请重试或使用系统键盘听写。');renderHeader();}
  });
  prompt.addEventListener('keydown',event=>{if(event.key==='Escape'&&current){event.preventDefault();stop();}});
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden&&current){cancel();status('语音输入已暂停，已识别文字已保留。');renderHeader();}
  });
  window.addEventListener('pagehide',cancel);
  render();
  return {active:()=>!!current,cancel,render,stop};
})();
