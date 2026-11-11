'use strict';

// Original bytes live in IndexedDB; the small memory cache avoids disk reads.
const filePreviewCache = new Map();
const filePreviewCacheTTL = 30 * 24 * 60 * 60 * 1000;
const filePreviewCacheKey = preview => JSON.stringify([currentUser?.id,preview.deviceId,preview.path]);
let fileCacheDatabase, fileCacheOperations = Promise.resolve();

function fileCacheDB() {
  if (!fileCacheDatabase) fileCacheDatabase = new Promise(resolve => {
    try {
      const request = indexedDB.open('relay-file-cache',1);
      request.onupgradeneeded = () => {
        request.result.createObjectStore('files',{keyPath:'key'}).createIndex('owner','owner');
        request.result.createObjectStore('contents');
      };
      request.onsuccess = () => { request.result.onversionchange = () => request.result.close(); resolve(request.result); };
      request.onerror = request.onblocked = () => resolve(null);
    } catch { resolve(null); }
  });
  return fileCacheDatabase;
}

function fileCacheOperation(action) {
  const next = fileCacheOperations.then(async () => { const db = await fileCacheDB(); return db ? action(db) : null; });
  // Storage restrictions and quota failures must not prevent previewing files.
  fileCacheOperations = next.catch(() => null);
  return fileCacheOperations;
}

function fileCacheTransaction(db, mode, action) {
  return new Promise((resolve,reject) => {
    const transaction = db.transaction(['files','contents'],mode), store = transaction.objectStore('files');
    let value;
    transaction.oncomplete = () => resolve(value);
    transaction.onerror = transaction.onabort = () => reject(transaction.error);
    action(store,result => { value = result; },transaction.objectStore('contents'));
  });
}

function clearFilePreviewCache(owner = currentUser?.id) {
  filePreviewCache.clear();
  return fileCacheOperation(db => fileCacheTransaction(db,'readwrite',(store,result,contents) => {
    const request = owner ? store.index('owner').openCursor(IDBKeyRange.only(owner)) : store.openCursor();
    request.onsuccess = () => { const cursor = request.result; if (cursor) { contents.delete(cursor.key); cursor.delete(); cursor.continue(); } };
  }));
}

function rememberPreviewFile(entry) {
  filePreviewCache.delete(entry.key); filePreviewCache.set(entry.key,entry);
  let bytes = [...filePreviewCache.values()].reduce((sum,item) => sum + item.file.data.byteLength,0);
  for (const [key,item] of filePreviewCache) {
    if (bytes <= 32 * 1024 * 1024 && filePreviewCache.size <= 20) break;
    filePreviewCache.delete(key); bytes -= item.file.data.byteLength;
  }
}

async function cachedPreviewFile(preview) {
  const key = filePreviewCacheKey(preview);
  let entry = filePreviewCache.get(key);
  if (!entry) {
    const stored = await fileCacheOperation(db => fileCacheTransaction(db,'readonly',(store,result,contents) => {
      const request = store.get(key);
      request.onsuccess = () => {
        const metadata = request.result;
        if (!metadata || Date.now()-metadata.checkedAt >= filePreviewCacheTTL) { result(metadata); return; }
        const bytes = contents.get(key);
        bytes.onsuccess = () => result({...metadata,file:{...metadata.file,data:bytes.result}});
      };
    }));
    if (!previewCurrent(preview)) return null;
    if (!stored) return cloudCachedPreviewFile(preview);
    if (Date.now() - stored.checkedAt >= filePreviewCacheTTL) {
      await fileCacheOperation(db => fileCacheTransaction(db,'readwrite',(store,result,contents) => { store.delete(key); contents.delete(key); }));
      return cloudCachedPreviewFile(preview);
    }
    entry = stored;
    if (!(entry.file.data instanceof ArrayBuffer)) entry = null;
    else {
      const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256',entry.file.data))].map(x => x.toString(16).padStart(2,'0')).join('');
      if (hash !== entry.file.sha256) entry = null;
    }
  }
  if (!entry || Date.now() - entry.checkedAt >= filePreviewCacheTTL) {
    filePreviewCache.delete(key);
    await fileCacheOperation(db => fileCacheTransaction(db,'readwrite',(store,result,contents) => { store.delete(key); contents.delete(key); }));
    return null;
  }
  if (!previewCurrent(preview)) return null;
  entry.usedAt = Date.now(); rememberPreviewFile(entry);
  persistPreviewFile(preview,entry);
  return entry;
}

function persistPreviewFile(preview, entry) {
  return fileCacheOperation(db => {
    if (!previewCurrent(preview)) return;
    return fileCacheTransaction(db,'readwrite',(store,result,contents) => {
      const previous = store.get(entry.key);
      previous.onsuccess = () => {
        const {data,...file} = entry.file;
        // ArrayBuffer works in Safari's IndexedDB; Blob serialization can fail.
        // Metadata checks never rewrite or enumerate cached file contents.
        if (previous.result?.file.sha256 !== file.sha256) contents.put(data,entry.key);
        store.put({...entry,file,byteLength:data.byteLength});
        const request = store.getAll();
        request.onsuccess = () => {
          const entries = request.result.sort((a,b) => b.usedAt-a.usedAt);
          let bytes = 0, count = 0;
          for (const item of entries) {
            const expired = Date.now()-item.checkedAt >= filePreviewCacheTTL;
            if (!expired) { bytes += item.byteLength; count++; }
            if (expired || count>100 || bytes>128*1024*1024) { store.delete(item.key); contents.delete(item.key); }
          }
        };
      };
    });
  });
}

async function cachePreviewFile(preview, file) {
  const now = Date.now();
  const entry = {key:filePreviewCacheKey(preview),owner:currentUser.id,file,fetchedAt:now,checkedAt:now,usedAt:now};
  rememberPreviewFile(entry);
  await persistPreviewFile(preview,entry);
  return entry;
}

// OSS is the shared cache; IndexedDB is only a local acceleration layer.
async function cloudCachedPreviewFile(preview) {
  const result = await api('files/cache?' + new URLSearchParams({device:preview.deviceId,path:preview.path}));
  if (!previewCurrent(preview) || !result.file) return null;
  const file = await readArtifact(result.file.id,result.file);
  if (!previewCurrent(preview)) return null;
  const now = Date.now(), metadata = result.file;
  const entry = {key:filePreviewCacheKey(preview),owner:currentUser.id,file,fetchedAt:metadata.created_at*1000,checkedAt:metadata.checked_at*1000,usedAt:now};
  rememberPreviewFile(entry); await persistPreviewFile(preview,entry);
  return entry;
}
