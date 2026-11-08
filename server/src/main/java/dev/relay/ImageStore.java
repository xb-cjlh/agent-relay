package dev.relay;

import org.springframework.stereotype.Component;
import org.springframework.http.*;
import javax.imageio.ImageIO;
import java.io.*;
import java.util.*;
import static dev.relay.Values.*;

@Component
final class ImageStore {
    static final int MAX_IMAGE=5*1024*1024, MAX_TOTAL=12*1024*1024;
    final Store store;
    final OssImages oss;
    ImageStore(Store store,OssImages oss){this.store=store;this.oss=oss;}

    Object initiate(Map<String,Object> body) {
        String name=required(body,"name",255),mime=required(body,"mime",32),sha=required(body,"sha256",64),md5=required(body,"md5",24);
        if(!Set.of("image/png","image/jpeg").contains(mime))throw new ApiException("请选择 PNG 或 JPEG 图片");
        int size=integer(body.get("size"),1,MAX_IMAGE,"图片大小");
        if(!sha.matches("[a-f0-9]{64}"))throw new ApiException("图片 SHA-256 无效");
        try{if(Base64.getDecoder().decode(md5).length!=16||!Base64.getEncoder().encodeToString(Base64.getDecoder().decode(md5)).equals(md5))throw new IllegalArgumentException();}
        catch(IllegalArgumentException e){throw new ApiException("图片 Content-MD5 无效");}
        cleanup();
        var usage=store.row("SELECT COUNT(*) n,COALESCE(SUM(size),0) bytes FROM image_uploads WHERE owner_user_id=? AND created_at>?",store.owner(),now()-86400);
        if(number(usage.get("n"))>=200||number(usage.get("bytes"))+size>200*1024*1024)throw new ApiException(429,"今日图片上传量已达上限");
        var pending=store.row("SELECT COUNT(*) n FROM image_uploads WHERE owner_user_id=? AND completed_at IS NULL AND expires_at>?",store.owner(),now());
        if(number(pending.get("n"))>=20)throw new ApiException(429,"待上传的图片过多，请稍后重试");
        String uploadId=id("upl"),imageId=id("img"),key="users/"+store.owner()+"/images/"+imageId;
        double at=now();long signedUntil=(long)((at+300)*1000),completeBefore=(long)((at+900)*1000);
        var result=oss.presign(key,size,mime,md5,sha,uploadId,signedUntil);
        store.update("INSERT INTO image_uploads(id,owner_user_id,image_id,object_key,name,mime,size,sha256,content_md5,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",uploadId,store.owner(),imageId,key,name,mime,size,sha,md5,at,completeBefore/1000.0);
        result.putAll(map("uploadId",uploadId,"method","PUT","expiresAt",signedUntil,"completeBefore",completeBefore));
        return result;
    }
    Object complete(String uploadId) {
        var upload=store.row("SELECT * FROM image_uploads WHERE id=? AND owner_user_id=? FOR UPDATE",uploadId,store.owner());
        String imageId=text(upload.get("image_id"));
        if(upload.get("completed_at")!=null)return metadata(imageId);
        if(number(upload.get("expires_at"))<now())throw new ApiException(410,"图片上传已过期，请重新选择");
        var head=oss.head(text(upload.get("object_key")));
        if(head==null)throw new ApiException(409,"图片尚未上传完成，请重试");
        var claims=head.getUserMetadata();
        if(head.getContentLength()!=(long)number(upload.get("size"))||!upload.get("mime").equals(head.getContentType())||
            !uploadId.equals(claims.get("upload-id"))||!upload.get("sha256").equals(claims.get("sha256"))||
            !upload.get("content_md5").equals(claims.get("content-md5"))||!Long.toString(head.getContentLength()).equals(claims.get("declared-size")))
            throw new ApiException(409,"图片与上传签名不一致，请重新选择");
        store.update("INSERT INTO images(id,owner_user_id,name,mime,size,sha256,created_at,object_key) VALUES(?,?,?,?,?,?,?,?)",imageId,store.owner(),upload.get("name"),upload.get("mime"),upload.get("size"),upload.get("sha256"),now(),upload.get("object_key"));
        store.update("UPDATE image_uploads SET completed_at=? WHERE id=?",now(),uploadId);
        return metadata(imageId);
    }
    private Object metadata(String id){return store.row("SELECT id,name,mime,size,sha256 FROM images WHERE id=? AND owner_user_id=?",id,store.owner());}
    private void cleanup() {
        for(var row:store.rows("SELECT id,object_key FROM images WHERE owner_user_id=? AND job_id IS NULL AND created_at<? LIMIT 20",store.owner(),now()-86400)) {
            oss.delete(text(row.get("object_key")));
            store.update("DELETE FROM image_uploads WHERE image_id=?",row.get("id"));
            store.update("DELETE FROM images WHERE id=? AND owner_user_id=? AND job_id IS NULL",row.get("id"),store.owner());
        }
        for(var row:store.rows("SELECT id,object_key FROM image_uploads WHERE owner_user_id=? AND completed_at IS NULL AND cleaned_at IS NULL AND expires_at<? LIMIT 20",store.owner(),now())) {
            oss.delete(text(row.get("object_key")));
            store.update("UPDATE image_uploads SET cleaned_at=? WHERE id=?",now(),row.get("id"));
        }
    }
    List<Object> validate(Object raw,String deviceId) {
        var ids=list(raw,4);List<Object> images=new ArrayList<>();Set<String> seen=new HashSet<>();int total=0;
        if(!ids.isEmpty()) {
            var device=store.device(deviceId);var caps=object(store.decode(device.get("capabilities")));
            if(!truth(caps.get("image_input")))throw new ApiException(409,"请将目标设备 Relay 升级至 0.5.0 并重启后发送图片");
        }
        for(Object value:ids) {
            String id=text(value);
            if(!seen.add(id))throw new ApiException("图片不能重复添加");
            var image=store.row("SELECT id,name,mime,size,sha256,job_id FROM images WHERE id=? AND owner_user_id=?",id,store.owner());
            if(image.remove("job_id")!=null)throw new ApiException(409,"图片已发送，请重新上传");
            total+=number(image.get("size"));images.add(image);
        }
        if(total>MAX_TOTAL)throw new ApiException(413,"每条消息的图片合计最大 12 MiB");
        return images;
    }
    Map<String,Object> previewUrl(String id) {
        var image=store.row("SELECT object_key,mime FROM images WHERE id=? AND owner_user_id=?",id,store.owner());
        long expires=System.currentTimeMillis()+300000;
        return map("url",oss.presignGet(text(image.get("object_key")),text(image.get("mime")),expires),"expiresAt",expires);
    }
    ResponseEntity<?> preview(String id) {
        return ResponseEntity.status(HttpStatus.FOUND).location(java.net.URI.create(text(previewUrl(id).get("url")))).cacheControl(CacheControl.noStore()).build();
    }
    ResponseEntity<?> download(String id,String deviceId) throws Exception {
        var image=store.row("SELECT i.* FROM images i JOIN jobs j ON j.id=i.job_id WHERE i.id=? AND j.device_id=? AND i.owner_user_id=?",id,deviceId,store.owner());
        byte[] bytes=oss.read(text(image.get("object_key")));
        if(bytes.length!=(int)number(image.get("size"))||!hash(bytes).equals(image.get("sha256")))throw new ApiException(409,"图片校验失败");
        validateBytes(bytes,text(image.get("mime")));
        return ResponseEntity.ok().contentType(MediaType.parseMediaType(text(image.get("mime")))).header("X-SHA256",text(image.get("sha256"))).body(bytes);
    }
    static void validateBytes(byte[] bytes,String mime) throws IOException {
        try(var input=ImageIO.createImageInputStream(new ByteArrayInputStream(bytes))) {
            var readers=ImageIO.getImageReaders(input);
            if(!readers.hasNext())throw new ApiException("图片格式无效");
            var reader=readers.next();
            try {
                reader.setInput(input);String format=reader.getFormatName().toLowerCase(Locale.ROOT);
                if(!(mime.equals("image/png")&&format.equals("png")||mime.equals("image/jpeg")&&Set.of("jpeg","jpg").contains(format)))throw new ApiException("图片格式不一致");
                long width=reader.getWidth(0),height=reader.getHeight(0);
                if(width<=0||height<=0||width*height>36_000_000)throw new ApiException("图片分辨率无效");
            }finally{reader.dispose();}
        }
    }
}
