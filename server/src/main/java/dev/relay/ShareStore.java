package dev.relay;

import org.springframework.stereotype.Component;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.*;
import static dev.relay.Values.*;

/** Immutable conversation and file snapshots; share secrets are never stored. */
@Component
final class ShareStore {
    static final int MAX_MESSAGES=5000, MAX_BYTES=8*1024*1024;
    final Store store;
    final OssImages oss;
    final SecureRandom random=new SecureRandom();
    ShareStore(Store store,OssImages oss){this.store=store;this.oss=oss;}

    Object create(String agentId,Map<String,Object> body) {
        var agent=store.agent(agentId);
        int duration=integer(body.getOrDefault("expiresIn",604800),60,2592000,"分享有效期（秒）");
        cleanup();
        var usage=store.row("SELECT COUNT(*) n,COALESCE(SUM(OCTET_LENGTH(snapshot)),0) bytes FROM conversation_shares WHERE owner_user_id=? AND revoked_at IS NULL AND expires_at>?",store.owner(),now());
        if(number(usage.get("n"))>=50||number(usage.get("bytes"))>64*1024*1024)throw new ApiException(429,"有效分享过多，请先撤销部分链接");
        var conversation=store.conversation(text(agent.get("id")),null,MAX_MESSAGES+1);
        var source=list(conversation.get("messages"),MAX_MESSAGES+1);
        if(truth(conversation.get("has_more"))||source.size()>MAX_MESSAGES)throw new ApiException(413,"会话过长，暂不支持分享超过 5000 条消息的会话");
        List<Map<String,Object>> messages=new ArrayList<>();Map<String,Object> resources=new LinkedHashMap<>();
        for(Object value:source) {
            var m=object(value);String role=text(m.get("role")),content=text(m.get("content"));
            if(!Set.of("user","assistant").contains(role))continue;
            if(role.equals("assistant")&&content.isBlank())continue;
            var item=map("id",m.get("id"),"role",role,"content",content,"created_at",m.get("created_at"));
            if(role.equals("user")) {
                for(String kind:List.of("images","files")) {
                    List<Object> attachments=new ArrayList<>();
                    for(Object raw:list(m.getOrDefault(kind,List.of()),8)) {
                        String id=text(object(raw).get("id"));boolean image=kind.equals("images");
                        var file=store.row("SELECT a.id,a.name,a.mime,a.size,a.object_key FROM "+(image?"images":"attachments")+" a JOIN jobs j ON j.id=a.job_id WHERE a.id=? AND a.owner_user_id=? AND j.agent_id=?",id,store.owner(),agent.get("id"));
                        resources.put(id,map("key",file.remove("object_key"),"name",file.get("name"),"mime",file.get("mime"),"image",image));
                        attachments.add(file);
                    }
                    if(!attachments.isEmpty())item.put(kind,attachments);
                }
            }
            var segments=role.equals("assistant")?list(m.getOrDefault("segments",List.of()),MAX_MESSAGES):List.of();
            if(segments.isEmpty())messages.add(item);
            else for(Object raw:segments) {
                var segment=object(raw);
                int start=(int)number(segment.get("start")),end=(int)number(segment.get("end"));
                messages.add(map("id",m.get("id"),"role",role,"content",content.substring(start,end),"created_at",segment.get("created_at"),"offset",start));
            }
            if(messages.size()>MAX_MESSAGES)throw new ApiException(413,"会话过长，暂不支持分享超过 5000 条消息的会话");
        }
        messages.sort(Comparator.<Map<String,Object>>comparingDouble(m->number(m.get("created_at"))).thenComparing(m->text(m.get("id"))).thenComparingDouble(m->number(m.getOrDefault("offset",0))));
        for(int i=0;i<messages.size();i++){messages.get(i).put("id",i);messages.get(i).remove("offset");}
        String title=text(agent.getOrDefault("title",""));if(title.isBlank())title=text(agent.get("name"));
        String snapshot=store.encode(map("title",title,"kind",agent.get("kind"),"messages",messages));
        if(snapshot.getBytes(StandardCharsets.UTF_8).length>MAX_BYTES)throw new ApiException(413,"会话内容超过 8 MiB，暂时无法分享");
        if(number(usage.get("bytes"))+snapshot.getBytes(StandardCharsets.UTF_8).length>64*1024*1024)throw new ApiException(429,"分享内容已达上限，请先撤销部分链接");
        var issued=issue("conversation",text(agent.get("id")),null,title,snapshot,resources,now()+duration);
        issued.put("message_count",messages.size());return issued;
    }
    private Map<String,Object> issue(String type,String agent,String resource,String title,String snapshot,Map<String,Object> resources,double expires) {
        byte[] secret=new byte[32];random.nextBytes(secret);
        String token="shr-relay_"+Base64.getUrlEncoder().withoutPadding().encodeToString(secret),id=id("shr");
        double at=now();
        store.update("INSERT INTO conversation_shares(id,owner_user_id,agent_id,token_hash,title,snapshot,resources,created_at,expires_at,share_type,resource_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",id,store.owner(),agent,hash(token),title,snapshot,store.encode(resources),at,expires,type,resource);
        return map("id",id,"token",token,"path","/share#"+token,"created_at",at,"expires_at",expires);
    }
    Object createFile(Map<String,Object> body) throws java.io.IOException {
        int duration=integer(body.getOrDefault("expiresIn",604800),60,2592000,"分享有效期（秒）");
        String id=required(body,"file",64);cleanup();
        var usage=store.row("SELECT COUNT(*) n,COALESCE(SUM(OCTET_LENGTH(snapshot)),0) bytes FROM conversation_shares WHERE owner_user_id=? AND revoked_at IS NULL AND expires_at>?",store.owner(),now());
        if(number(usage.get("n"))>=50)throw new ApiException(429,"有效分享过多，请先撤销部分链接");
        double expires=now()+duration;
        var file=fileResource(id,expires);
        String key=text(file.remove("key"));
        var resources=map("file",map("key",key,"name",file.get("name")));
        String snapshot=store.encode(map("file",file));
        if(number(usage.get("bytes"))+snapshot.getBytes(StandardCharsets.UTF_8).length>64*1024*1024)throw new ApiException(429,"分享内容已达上限，请先撤销部分链接");
        var result=issue("file",null,id,text(file.get("name")),snapshot,resources,expires);
        result.put("file",file);return result;
    }
    private Map<String,Object> fileResource(String id,double expires) throws java.io.IOException {
        if(!oss.configured())throw new ApiException(503,"请先配置 OSS 文件存储后再分享文件");
        if(id.matches("file_[a-zA-Z0-9]+"))return store.fileStore.shareResource(id,expires);
        String table=id.matches("att_[a-zA-Z0-9]+")?"attachments":id.matches("img_[a-zA-Z0-9]+")?"images":null;
        if(table==null)throw new ApiException("文件 ID 无效");
        var file=store.row("SELECT id,name,size,sha256,object_key,job_id FROM "+table+" WHERE id=? AND owner_user_id=?",id,store.owner());
        if(file.remove("job_id")==null)throw new ApiException(409,"请先发送附件，再创建分享");
        String key=text(file.remove("object_key"));var head=oss.head(key);
        if(head==null)throw new ApiException(404,"文件已不可用");
        if(head.getContentLength()!=(long)number(file.get("size"))||!file.get("sha256").equals(head.getUserMetadata().get("sha256")))throw new ApiException(409,"文件校验失败");
        file.put("key",key);return file;
    }
    Object listFileShares(String id) {
        if(id==null||!id.matches("(file|att|img)_[a-zA-Z0-9]+"))throw new ApiException("文件 ID 无效");
        if(id.startsWith("file_"))store.fileStore.artifact(id);
        else store.row("SELECT id FROM "+(id.startsWith("att_")?"attachments":"images")+" WHERE id=? AND owner_user_id=?",id,store.owner());
        return map("shares",store.rows("SELECT id,title,created_at,expires_at,revoked_at FROM conversation_shares WHERE resource_id=? AND share_type='file' AND owner_user_id=? AND revoked_at IS NULL AND expires_at>? ORDER BY created_at DESC LIMIT 50",id,store.owner(),now()));
    }
    Object listShares(String agentId) {
        var agent=store.agent(agentId);
        return map("shares",store.rows("SELECT id,title,created_at,expires_at,revoked_at FROM conversation_shares WHERE agent_id=? AND owner_user_id=? AND revoked_at IS NULL AND expires_at>? ORDER BY created_at DESC LIMIT 50",agent.get("id"),store.owner(),now()));
    }
    Object revoke(String id) {
        store.row("SELECT id FROM conversation_shares WHERE id=? AND owner_user_id=?",id,store.owner());
        store.update("UPDATE conversation_shares SET revoked_at=COALESCE(revoked_at,?),snapshot='{}',resources='{}' WHERE id=? AND owner_user_id=?",now(),id,store.owner());
        return map("ok",true);
    }
    Map<String,Object> resolve(String authorization,boolean conversation) {
        String token=authorization!=null&&authorization.startsWith("Bearer ")?authorization.substring(7):"";
        if(!token.matches("shr-relay_[A-Za-z0-9_-]{43}"))throw unavailable();
        var found=store.rows("SELECT share_type,expires_at,resources"+(conversation?",snapshot,created_at":"")+" FROM conversation_shares WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?",hash(token),now());
        if(found.isEmpty())throw unavailable();
        return found.get(0);
    }
    Object view(Map<String,Object> share,String before) {
        requireType(share,"conversation");
        var snapshot=object(store.decode(share.get("snapshot")));var messages=list(snapshot.get("messages"),MAX_MESSAGES);
        int end=messages.size();
        if(before!=null) {
            try {end=Integer.parseInt(before);}catch(NumberFormatException e){throw new ApiException("分页参数无效");}
            if(end<0||end>messages.size())throw new ApiException("分页参数无效");
        }
        int start=Math.max(0,end-100);
        return map("title",snapshot.get("title"),"kind",snapshot.get("kind"),"created_at",share.get("created_at"),"expires_at",share.get("expires_at"),"messages",messages.subList(start,end),"has_more",start>0,"next_before",start);
    }
    Object resource(Map<String,Object> share,String id,boolean image) {
        requireType(share,"conversation");
        var resource=object(store.decode(share.get("resources"))).get(id);
        if(resource==null||truth(object(resource).get("image"))!=image)throw new ApiException(404,"此附件不在分享范围内");
        var file=object(resource);
        long expires=Math.min(System.currentTimeMillis()+300000,(long)(number(share.get("expires_at"))*1000));
        String url=image?oss.presignGet(text(file.get("key")),text(file.get("mime")),expires):oss.presignFileGet(text(file.get("key")),text(file.get("name")),expires);
        return map("url",url,"expiresAt",expires);
    }
    Object content(Map<String,Object> share,String before) {
        if("conversation".equals(share.get("share_type")))return view(share,before);
        requireType(share,"file");
        var snapshot=object(store.decode(share.get("snapshot")));
        return map("type","file","file",snapshot.get("file"),"created_at",share.get("created_at"),"expires_at",share.get("expires_at"));
    }
    Object fileUrl(Map<String,Object> share) {
        requireType(share,"file");var file=object(object(store.decode(share.get("resources"))).get("file"));
        long expires=Math.min(System.currentTimeMillis()+300000,(long)(number(share.get("expires_at"))*1000));
        return map("url",oss.presignFileGet(text(file.get("key")),text(file.get("name")),expires),"expiresAt",expires);
    }
    private void requireType(Map<String,Object> share,String type) {if(!type.equals(share.get("share_type")))throw new ApiException(404,"此内容不在分享范围内");}
    void cleanup() {
        store.update("UPDATE conversation_shares SET snapshot='{}',resources='{}' WHERE owner_user_id=? AND (expires_at<=? OR revoked_at IS NOT NULL) AND snapshot<>'{}'",store.owner(),now());
    }
    private ApiException unavailable(){return new ApiException(404,"分享链接已失效或不存在");}
}
