package dev.relay;

import java.nio.file.*;
import java.util.*;
import static dev.relay.Values.*;

/** Account-scoped originals in OSS; the database holds paths, hashes and expiry. */
final class FileStore {
    static final double TTL=30*86400;
    private final Store store;
    private final OssImages oss;
    FileStore(Store store,OssImages oss){this.store=store;this.oss=oss;}

    private String filename(Map<String,Object> body) {
        String name=required(body,"name",255);
        if(name.contains("/")||name.contains("\\")||name.equals(".")||name.equals("..")||name.chars().anyMatch(c->c<32))throw new ApiException("文件名无效");
        return name;
    }
    Map<String,Object> artifact(String id) {
        return store.row("SELECT f.*,o.object_key,o.checked_at,o.expires_at FROM artifacts f LEFT JOIN file_objects o ON o.artifact_id=f.id JOIN jobs j ON j.id=f.job_id JOIN devices d ON d.id=j.device_id WHERE f.id=? AND d.owner_user_id=?",id,store.owner());
    }
    private boolean usable(Map<String,Object> file) {
        if(file.get("expires_at")!=null&&number(file.get("expires_at"))<=now())return false;
        return file.get("object_key")!=null ? oss.head(text(file.get("object_key")))!=null : Files.isRegularFile(store.files.resolve(text(file.get("id"))));
    }
    private Map<String,Object> latest(String device,String path) {
        if(!oss.configured())return null;
        var found=store.rows("SELECT f.*,o.object_key,o.checked_at,o.expires_at FROM file_cache c JOIN artifacts f ON f.id=c.artifact_id JOIN file_objects o ON o.artifact_id=f.id WHERE c.device_id=? AND c.path_hash=? AND c.path=? AND o.expires_at>?",device,hash(path),path,now());
        return found.isEmpty()||!usable(found.get(0)) ? null : found.get(0);
    }
    private Map<String,Object> metadata(Map<String,Object> file) {
        return map("id",file.get("id"),"name",file.get("name"),"size",file.get("size"),"sha256",file.get("sha256"),"created_at",file.get("created_at"),"checked_at",file.get("checked_at"),"expires_at",file.get("expires_at"));
    }
    Object cached(String device,String path) {
        String deviceId=text(store.device(device).get("id"));
        var file=latest(deviceId,path);
        return file==null ? null : metadata(file);
    }
    void prepare(String device,Map<String,Object> payload,Map<String,Object> body) {
        Map<String,Object> file;
        if(body.containsKey("cached_artifact")) {
            String cached=required(body,"cached_artifact",64);
            file=store.row("SELECT f.*,o.object_key,o.checked_at,o.expires_at,j.payload original_payload FROM artifacts f LEFT JOIN file_objects o ON o.artifact_id=f.id JOIN jobs j ON j.id=f.job_id WHERE f.id=? AND j.device_id=?",cached,device);
            if(!payload.get("path").equals(object(store.decode(file.get("original_payload"))).get("path")))throw new ApiException("缓存不属于此文件路径");
            if(!usable(file))throw new ApiException(404,"缓存文件已失效");
        } else file=latest(device,text(payload.get("path")));
        if(file!=null){payload.put("cached_artifact",file.get("id"));payload.put("if_sha256",file.get("sha256"));}
    }
    Object initiate(String device,String jobId,Map<String,Object> body) {
        var job=store.job(jobId);
        if(!device.equals(job.get("device_id"))||!"file_read".equals(job.get("kind")))throw new ApiException(404,"找不到文件任务");
        if(!"running".equals(job.get("status")))throw new ApiException(409,"文件任务已停止");
        if(!oss.configured())return map("supported",false);
        String name=filename(body),sha=required(body,"sha256",64),md5=required(body,"md5",24);
        int size=integer(body.get("size"),0,MAX_FILE,"文件大小");
        if(!sha.matches("[a-f0-9]{64}"))throw new ApiException("文件 SHA-256 无效");
        try {if(Base64.getDecoder().decode(md5).length!=16||!Base64.getEncoder().encodeToString(Base64.getDecoder().decode(md5)).equals(md5))throw new IllegalArgumentException();}
        catch(IllegalArgumentException e){throw new ApiException("文件 Content-MD5 无效");}
        var found=store.rows("SELECT * FROM file_uploads WHERE job_id=?",jobId);
        Map<String,Object> upload;
        if(found.isEmpty()) {
            String uploadId=id("fup"),artifactId=id("file"),key="users/"+store.owner()+"/files/"+artifactId;
            store.update("INSERT INTO file_uploads(id,job_id,artifact_id,object_key,name,size,sha256,content_md5,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",uploadId,jobId,artifactId,key,name,size,sha,md5,now()+900);
            upload=store.row("SELECT * FROM file_uploads WHERE id=?",uploadId);
        } else {
            upload=found.get(0);
            if(!name.equals(upload.get("name"))||size!=(int)number(upload.get("size"))||!sha.equals(upload.get("sha256"))||!md5.equals(upload.get("content_md5")))throw new ApiException(409,"文件上传内容已改变");
            if(number(upload.get("expires_at"))<=now())throw new ApiException(410,"文件上传已过期");
        }
        long expires=Math.min(System.currentTimeMillis()+300000,(long)(number(upload.get("expires_at"))*1000));
        var ticket=oss.presign(text(upload.get("object_key")),size,"application/octet-stream",md5,sha,text(upload.get("id")),expires);
        ticket.putAll(map("supported",true,"upload_id",upload.get("id"),"expiresAt",expires));
        return ticket;
    }
    String complete(String device,Map<String,Object> job,Map<String,Object> file) throws java.io.IOException {
        String jobId=text(job.get("id"));var payload=object(store.decode(job.get("payload")));
        String artifactId;
        if(truth(file.get("unchanged"))) {
            var previous=artifact(required(payload,"cached_artifact",64));
            if(!secureEquals(text(previous.get("sha256")),text(file.get("sha256")))||!usable(previous))throw new ApiException("文件缓存校验失败");
            artifactId=text(previous.get("id"));
        } else if(file.containsKey("upload_id")) {
            var upload=store.row("SELECT * FROM file_uploads WHERE id=? AND job_id=?",required(file,"upload_id",64),jobId);
            if(number(upload.get("expires_at"))<=now())throw new ApiException(410,"文件上传已过期");
            var head=oss.head(text(upload.get("object_key")));
            if(head==null)throw new ApiException(409,"文件尚未上传完成");
            var claims=head.getUserMetadata();
            if(head.getContentLength()!=(long)number(upload.get("size"))||!"application/octet-stream".equals(head.getContentType())||!upload.get("id").equals(claims.get("upload-id"))||!upload.get("sha256").equals(claims.get("sha256"))||!upload.get("content_md5").equals(claims.get("content-md5"))||!Long.toString(head.getContentLength()).equals(claims.get("declared-size")))throw new ApiException(409,"文件与上传签名不一致");
            artifactId=text(upload.get("artifact_id"));
            insert(artifactId,jobId,text(upload.get("name")),(long)number(upload.get("size")),text(upload.get("sha256")),text(upload.get("object_key")));
        } else {
            String name=filename(file);byte[] content;
            try {content=Base64.getDecoder().decode(text(file.get("data")));}
            catch(IllegalArgumentException e){throw new ApiException("文件编码无效");}
            if(content.length>MAX_FILE)throw new ApiException(413,"文件超过 16 MiB 限制");
            String sha=hash(content);if(!secureEquals(sha,text(file.get("sha256"))))throw new ApiException("文件校验失败");
            var previous=payload.containsKey("cached_artifact") ? artifact(text(payload.get("cached_artifact"))) : null;
            if(previous!=null&&sha.equals(previous.get("sha256"))&&usable(previous))artifactId=text(previous.get("id"));
            else {
                artifactId=id("file");String key=oss.configured()?"users/"+store.owner()+"/files/"+artifactId:null;
                if(key!=null)oss.writeFile(key,content,sha);
                else {Path target=store.files.resolve(artifactId);Files.write(target,content,StandardOpenOption.CREATE_NEW);Store.privateFile(target);}
                insert(artifactId,jobId,name,content.length,sha,key);
            }
        }
        var result=artifact(artifactId);
        promote(result);
        if(result.get("object_key")!=null) {
            store.update("UPDATE file_objects SET checked_at=?,expires_at=? WHERE artifact_id=?",now(),now()+TTL,artifactId);
            String path=required(payload,"path");
            store.update("INSERT INTO file_cache(device_id,path_hash,path,artifact_id) VALUES(?,?,?,?) ON DUPLICATE KEY UPDATE path=VALUES(path),artifact_id=VALUES(artifact_id)",device,hash(path),path,artifactId);
        }
        return artifactId;
    }
    private void insert(String id,String job,String name,long size,String sha,String key) {
        store.update("INSERT INTO artifacts(id,job_id,name,size,sha256,created_at) VALUES(?,?,?,?,?,?)",id,job,name,size,sha,now());
        if(key!=null)store.update("INSERT INTO file_objects(artifact_id,object_key,checked_at,expires_at) VALUES(?,?,?,?)",id,key,now(),now()+TTL);
    }
    private void promote(Map<String,Object> file) throws java.io.IOException {
        if(!oss.configured()||file.get("object_key")!=null||file.get("expires_at")!=null)return;
        byte[] content=read(file);String key="users/"+store.owner()+"/files/"+file.get("id");
        oss.writeFile(key,content,text(file.get("sha256")));
        double checked=number(file.get("created_at")),expires=now()+TTL;
        store.update("INSERT INTO file_objects(artifact_id,object_key,checked_at,expires_at) VALUES(?,?,?,?)",file.get("id"),key,checked,expires);
        file.putAll(map("object_key",key,"checked_at",checked,"expires_at",expires));
    }
    Map<String,Object> url(String id) throws java.io.IOException {
        var file=artifact(id); promote(file);
        if(file.get("expires_at")!=null&&number(file.get("expires_at"))<=now())throw new ApiException(404,"缓存文件已失效");
        var result=metadata(file);long expires=System.currentTimeMillis()+300000;
        boolean remote=file.get("object_key")!=null;
        result.putAll(map("storage",remote?"oss":"local","url",remote?oss.presignFileGet(text(file.get("object_key")),text(file.get("name")),Math.min(expires,(long)(number(file.get("expires_at"))*1000))):"/api/files/"+id,"expiresAt",expires));
        return result;
    }
    byte[] read(Map<String,Object> file) throws java.io.IOException {
        if(file.get("expires_at")!=null&&number(file.get("expires_at"))<=now())throw new ApiException(404,"缓存文件已失效");
        byte[] bytes=file.get("object_key")!=null ? oss.readFile(text(file.get("object_key"))) : Files.readAllBytes(store.files.resolve(text(file.get("id"))));
        if(bytes.length!=(long)number(file.get("size"))||!hash(bytes).equals(file.get("sha256")))throw new ApiException(409,"文件校验失败");
        return bytes;
    }
    void cleanup() {
        if(!oss.configured())return;
        for(var file:store.rows("SELECT artifact_id id,object_key FROM file_objects WHERE object_key IS NOT NULL AND expires_at<=? LIMIT 20",now())) {
            oss.delete(text(file.get("object_key")));
            store.update("DELETE FROM file_cache WHERE artifact_id=?",file.get("id"));
            store.update("UPDATE file_objects SET object_key=NULL WHERE artifact_id=?",file.get("id"));
        }
        for(var upload:store.rows("SELECT u.id,u.object_key FROM file_uploads u WHERE u.cleaned_at IS NULL AND u.expires_at<=? AND NOT EXISTS(SELECT 1 FROM artifacts f WHERE f.id=u.artifact_id) LIMIT 20",now())) {
            oss.delete(text(upload.get("object_key")));store.update("UPDATE file_uploads SET cleaned_at=? WHERE id=?",now(),upload.get("id"));
        }
    }
}
