package dev.relay;

import org.springframework.stereotype.Component;
import org.springframework.http.*;
import java.util.*;
import static dev.relay.Values.*;

@Component
final class AttachmentStore {
    static final int MAX_ATTACHMENT=MAX_FILE, MAX_TOTAL=32*1024*1024;
    final Store store;
    final OssImages oss;
    AttachmentStore(Store store,OssImages oss){this.store=store;this.oss=oss;}

    Object initiate(Map<String,Object> body) {
        String name=safeName(required(body,"name",255)),mime="application/octet-stream",sha=required(body,"sha256",64),md5=required(body,"md5",24);
        int size=integer(body.get("size"),0,MAX_ATTACHMENT,"附件大小");
        if(!sha.matches("[a-f0-9]{64}"))throw new ApiException("附件 SHA-256 无效");
        try{if(Base64.getDecoder().decode(md5).length!=16||!Base64.getEncoder().encodeToString(Base64.getDecoder().decode(md5)).equals(md5))throw new IllegalArgumentException();}
        catch(IllegalArgumentException e){throw new ApiException("附件 Content-MD5 无效");}
        cleanup();
        var usage=store.row("SELECT COUNT(*) n,COALESCE(SUM(size),0) bytes FROM attachment_uploads WHERE owner_user_id=? AND created_at>?",store.owner(),now()-86400);
        if(number(usage.get("n"))>=200||number(usage.get("bytes"))+size>512*1024*1024)throw new ApiException(429,"今日附件上传量已达上限");
        var pending=store.row("SELECT COUNT(*) n FROM attachment_uploads WHERE owner_user_id=? AND completed_at IS NULL AND expires_at>?",store.owner(),now());
        if(number(pending.get("n"))>=20)throw new ApiException(429,"待上传的附件过多，请稍后重试");
        String uploadId=id("upl"),attachmentId=id("att"),key="users/"+store.owner()+"/attachments/"+attachmentId;
        double at=now();long signedUntil=(long)((at+300)*1000),completeBefore=(long)((at+900)*1000);
        var result=oss.presign(key,size,mime,md5,sha,uploadId,signedUntil);
        store.update("INSERT INTO attachment_uploads(id,owner_user_id,attachment_id,object_key,name,mime,size,sha256,content_md5,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",uploadId,store.owner(),attachmentId,key,name,mime,size,sha,md5,at,completeBefore/1000.0);
        result.putAll(map("uploadId",uploadId,"method","PUT","expiresAt",signedUntil,"completeBefore",completeBefore));
        return result;
    }
    Object complete(String uploadId) {
        var upload=store.row("SELECT * FROM attachment_uploads WHERE id=? AND owner_user_id=? FOR UPDATE",uploadId,store.owner());
        String attachmentId=text(upload.get("attachment_id"));
        if(upload.get("completed_at")!=null)return metadata(attachmentId);
        if(number(upload.get("expires_at"))<now())throw new ApiException(410,"附件上传已过期，请重新选择");
        var head=oss.head(text(upload.get("object_key")));
        if(head==null)throw new ApiException(409,"附件尚未上传完成，请重试");
        var claims=head.getUserMetadata();
        if(head.getContentLength()!=(long)number(upload.get("size"))||!upload.get("mime").equals(head.getContentType())||
            !uploadId.equals(claims.get("upload-id"))||!upload.get("sha256").equals(claims.get("sha256"))||
            !upload.get("content_md5").equals(claims.get("content-md5"))||!Long.toString(head.getContentLength()).equals(claims.get("declared-size")))
            throw new ApiException(409,"附件与上传签名不一致，请重新选择");
        store.update("INSERT INTO attachments(id,owner_user_id,name,mime,size,sha256,created_at,object_key) VALUES(?,?,?,?,?,?,?,?)",attachmentId,store.owner(),upload.get("name"),upload.get("mime"),upload.get("size"),upload.get("sha256"),now(),upload.get("object_key"));
        store.update("UPDATE attachment_uploads SET completed_at=? WHERE id=?",now(),uploadId);
        return metadata(attachmentId);
    }
    private Object metadata(String id){return store.row("SELECT id,name,mime,size,sha256 FROM attachments WHERE id=? AND owner_user_id=?",id,store.owner());}
    private void cleanup() {
        for(var row:store.rows("SELECT id,object_key FROM attachments WHERE owner_user_id=? AND job_id IS NULL AND created_at<? LIMIT 20",store.owner(),now()-86400)) {
            oss.delete(text(row.get("object_key")));
            store.update("DELETE FROM attachment_uploads WHERE attachment_id=?",row.get("id"));
            store.update("DELETE FROM attachments WHERE id=? AND owner_user_id=? AND job_id IS NULL",row.get("id"),store.owner());
        }
        for(var row:store.rows("SELECT id,object_key FROM attachment_uploads WHERE owner_user_id=? AND completed_at IS NULL AND cleaned_at IS NULL AND expires_at<? LIMIT 20",store.owner(),now())) {
            oss.delete(text(row.get("object_key")));
            store.update("UPDATE attachment_uploads SET cleaned_at=? WHERE id=?",now(),row.get("id"));
        }
    }
    List<Object> validate(Object raw,String deviceId) {
        var ids=list(raw,8);List<Object> attachments=new ArrayList<>();Set<String> seen=new HashSet<>();int total=0;
        if(!ids.isEmpty()) {
            var device=store.device(deviceId);var caps=object(store.decode(device.get("capabilities")));
            if(!truth(caps.get("file_input")))throw new ApiException(409,"请将目标设备 Relay 升级至 0.6.9 并重启后发送附件");
        }
        for(Object value:ids) {
            String id=text(value);
            if(!seen.add(id))throw new ApiException("附件不能重复添加");
            var file=store.row("SELECT id,name,mime,size,sha256,job_id FROM attachments WHERE id=? AND owner_user_id=?",id,store.owner());
            if(file.remove("job_id")!=null)throw new ApiException(409,"附件已发送，请重新上传");
            total+=number(file.get("size"));attachments.add(file);
        }
        if(total>MAX_TOTAL)throw new ApiException(413,"每条消息的附件合计最大 32 MiB");
        return attachments;
    }
    Map<String,Object> previewUrl(String id) {
        var file=store.row("SELECT object_key,name FROM attachments WHERE id=? AND owner_user_id=?",id,store.owner());
        long expires=System.currentTimeMillis()+300000;
        return map("url",oss.presignFileGet(text(file.get("object_key")),text(file.get("name")),expires),"expiresAt",expires);
    }
    ResponseEntity<?> preview(String id) {
        return ResponseEntity.status(HttpStatus.FOUND).location(java.net.URI.create(text(previewUrl(id).get("url")))).cacheControl(CacheControl.noStore()).build();
    }
    Map<String,Object> downloadUrl(String id,String deviceId) {
        var file=store.row("SELECT a.object_key,a.name FROM attachments a JOIN jobs j ON j.id=a.job_id WHERE a.id=? AND j.device_id=? AND a.owner_user_id=?",id,deviceId,store.owner());
        long expires=System.currentTimeMillis()+300000;
        return map("url",oss.presignFileGet(text(file.get("object_key")),text(file.get("name")),expires),"expiresAt",expires);
    }
    static String safeName(String name) {
        name=name.replace('\\','/');name=name.substring(name.lastIndexOf('/')+1);
        name=name.replaceAll("[\\p{Cntrl}<>:\"/\\\\|?*]","_").strip();
        while(name.getBytes(java.nio.charset.StandardCharsets.UTF_8).length>200)name=name.substring(0,name.offsetByCodePoints(name.length(),-1));
        return name.isBlank()||name.equals(".")||name.equals("..")?"attachment":name;
    }
}
