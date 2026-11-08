/* Images are resized and hashed locally; only the compressed Blob reaches OSS. */
window.RelayImages = (() => {
  const targetBytes = 1024 * 1024, maxEdge = 2048;
  const encode = (canvas,type,quality) => new Promise((resolve,reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('无法压缩图片')),type,quality));
  function check(signal) { if (signal?.aborted) throw new DOMException('已取消图片上传','AbortError'); }
  async function decode(file) {
    if (typeof createImageBitmap === 'function') {
      try { return await createImageBitmap(file); } catch {}
    }
    const url = URL.createObjectURL(file), image = new Image();
    try {
      image.src = url; await image.decode();
      return {width:image.naturalWidth,height:image.naturalHeight,image,close:()=>URL.revokeObjectURL(url)};
    } catch { URL.revokeObjectURL(url); throw new Error('浏览器无法读取这张图片，请转换为 PNG 或 JPEG'); }
  }
  async function prepare(file,signal) {
    check(signal);
    if (!file.size || file.size > 30*1024*1024) throw new Error('原始图片最大 30 MiB，请缩小后上传');
    const bitmap = await decode(file);
    let blob, width, height;
    try {
      check(signal);
      if (!bitmap.width || !bitmap.height || bitmap.width*bitmap.height > 100000000) throw new Error('图片分辨率过大，请先缩小图片');
      const scale = Math.min(1,maxEdge/Math.max(bitmap.width,bitmap.height));
      width = Math.max(1,Math.round(bitmap.width*scale)); height = Math.max(1,Math.round(bitmap.height*scale));
      if (scale===1 && ((file.type==='image/png' && file.size<=targetBytes) || (file.type==='image/jpeg' && file.size<=256*1024))) {
        blob = file;
      } else {
        const canvas = document.createElement('canvas'); canvas.width=width; canvas.height=height;
        const context = canvas.getContext('2d');
        context.drawImage(bitmap.image||bitmap,0,0,width,height);
        if (file.type==='image/png') {
          const png = await encode(canvas,'image/png');
          if (png.size<=targetBytes) blob=png;
        }
        if (!blob) {
          // Flatten transparency on white before converting to JPEG.
          context.globalCompositeOperation='destination-over'; context.fillStyle='#fff'; context.fillRect(0,0,width,height);
          for (const quality of [0.84,0.74,0.64]) {
            check(signal); blob=await encode(canvas,'image/jpeg',quality);
            if (blob.size<=targetBytes) break;
          }
          while (blob.size>targetBytes && Math.max(width,height)>512) {
            check(signal);
            width=Math.max(1,Math.round(width*0.8)); height=Math.max(1,Math.round(height*0.8));
            const smaller=document.createElement('canvas');smaller.width=width;smaller.height=height;
            smaller.getContext('2d').drawImage(canvas,0,0,width,height);
            blob=await encode(smaller,'image/jpeg',0.74);
            canvas.width=width;canvas.height=height;canvas.getContext('2d').drawImage(smaller,0,0);
          }
        }
      }
    } finally { bitmap.close(); }
    check(signal);
    if (blob.size>targetBytes) throw new Error('压缩后的图片仍然过大，请裁剪后再上传');
    const buffer=await blob.arrayBuffer();
    const sha256=[...new Uint8Array(await crypto.subtle.digest('SHA-256',buffer))].map(n=>n.toString(16).padStart(2,'0')).join('');
    const md5=btoa(SparkMD5.ArrayBuffer.hash(buffer,true));
    const suffix=blob.type==='image/png'?'.png':'.jpg';
    const name=(file.name||'粘贴的图片').replace(/\.[^.]+$/,'').slice(0,245)+suffix;
    return {blob,name,mime:blob.type,size:blob.size,sha256,md5,width,height};
  }
  async function upload(prepared,api,signal) {
    check(signal);
    const {blob,name,mime,size,sha256,md5}=prepared;
    const ticket=await api('images/uploads',{name,mime,size,sha256,md5});
    check(signal);
    let putError;
    try {
      const response=await fetch(ticket.url,{method:'PUT',body:blob,headers:ticket.headers,credentials:'omit',mode:'cors',redirect:'error',referrerPolicy:'no-referrer',signal});
      if (!response.ok) throw new Error(`图片直传失败 (${response.status})`);
    } catch(error) { putError=error; }
    check(signal);
    // Confirm even after a lost PUT response. Completion is idempotent.
    for (let attempt=0;attempt<2;attempt++) {
      try { return await api('images/uploads/'+encodeURIComponent(ticket.uploadId)+'/complete',{}); }
      catch(error) {
        if (!attempt && (!error.status || error.status>=500)) {check(signal);continue;}
        if (putError && error.status===409) throw new Error('图片直传失败，请检查网络后重新选择图片');
        throw error;
      }
    }
  }
  return {prepare,upload};
})();
