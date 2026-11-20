package dev.relay;

import org.springframework.stereotype.Component;
import java.util.*;
import static dev.relay.Values.*;

/** Durable inbox and transactional push outbox, both account scoped. */
@Component
final class NotificationStore {
    final Store store;
    final WebPushClient push;
    NotificationStore(Store store,WebPushClient push){this.store=store;this.push=push;}
    Object create(Map<String,Object> body) {
        String title=required(body,"title",120),message=required(body,"body",2000);
        String agent=body.containsKey("agent")?text(store.agent(required(body,"agent",64)).get("id")):null;
        String job=null;
        if(body.containsKey("job")) {
            var task=store.job(required(body,"job",64));job=text(task.get("id"));
            if(agent!=null&&!agent.equals(task.get("agent_id")))throw new ApiException("任务与会话不匹配");
            if(task.get("agent_id")!=null)agent=text(task.get("agent_id"));
        }
        String key=body.containsKey("idempotencyKey")?required(body,"idempotencyKey",200):token();
        String dedupe=hash("manual:"+key);
        var previous=store.rows("SELECT * FROM notifications WHERE owner_user_id=? AND dedupe_hash=?",store.owner(),dedupe);
        if(!previous.isEmpty()) {
            var n=previous.get(0);
            if(!title.equals(n.get("title"))||!message.equals(n.get("body"))||!Objects.equals(agent,n.get("agent_id"))||!Objects.equals(job,n.get("job_id")))throw new ApiException(409,"幂等键已用于另一条通知");
            return result(n,true);
        }
        if(number(store.row("SELECT COUNT(*) n FROM notifications WHERE owner_user_id=? AND source='agent' AND created_at>?",store.owner(),now()-3600).get("n"))>=60)throw new ApiException(429,"每小时最多发送 60 条主动通知");
        return result(issue(title,message,agent,job,"agent",dedupe),false);
    }
    Map<String,Object> issue(String title,String body,String agent,String job,String source,String dedupe) {
        var existing=store.rows("SELECT * FROM notifications WHERE owner_user_id=? AND dedupe_hash=?",store.owner(),dedupe);
        if(!existing.isEmpty())return existing.get(0);
        String id=id("ntf");double at=now();
        store.update("INSERT INTO notifications(id,owner_user_id,title,body,agent_id,job_id,source,dedupe_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",id,store.owner(),title,body,agent,job,source,dedupe,at);
        store.update("INSERT INTO push_deliveries(notification_id,subscription_id,next_attempt) SELECT ?,d.id,? FROM web_push_subscriptions d JOIN auth_sessions s ON s.token_hash=d.session_hash AND s.user_id=d.owner_user_id AND s.expires_at>? WHERE d.owner_user_id=? AND d.enabled=TRUE",id,at,at,store.owner());
        return store.row("SELECT * FROM notifications WHERE id=?",id);
    }
    Object result(Map<String,Object> notification,boolean duplicate) {
        var value=new LinkedHashMap<>(notification);value.remove("owner_user_id");value.remove("dedupe_hash");
        value.put("duplicate",duplicate);value.put("push_configured",push.configured());
        value.put("push_queued",number(store.row("SELECT COUNT(*) n FROM push_deliveries WHERE notification_id=? AND status='pending'",notification.get("id")).get("n")));
        return value;
    }
    Object list(String cursor) {
        List<Map<String,Object>> rows;
        if(cursor==null)rows=store.rows("SELECT id,title,body,agent_id,job_id,source,created_at,read_at FROM notifications WHERE owner_user_id=? ORDER BY created_at DESC,id DESC LIMIT 51",store.owner());
        else {
            var before=store.row("SELECT created_at,id FROM notifications WHERE id=? AND owner_user_id=?",cursor,store.owner());
            rows=store.rows("SELECT id,title,body,agent_id,job_id,source,created_at,read_at FROM notifications WHERE owner_user_id=? AND (created_at<? OR (created_at=? AND id<?)) ORDER BY created_at DESC,id DESC LIMIT 51",store.owner(),before.get("created_at"),before.get("created_at"),cursor);
        }
        boolean more=rows.size()>50;var page=rows.subList(0,Math.min(50,rows.size()));
        return map("notifications",page,"has_more",more,"next_cursor",more?page.get(page.size()-1).get("id"):null,"unread",store.row("SELECT COUNT(*) n FROM notifications WHERE owner_user_id=? AND read_at IS NULL",store.owner()).get("n"),"push_configured",push.configured());
    }
    Object get(String id) { return store.row("SELECT id,title,body,agent_id,job_id,source,created_at,read_at FROM notifications WHERE id=? AND owner_user_id=?",id,store.owner()); }
    Object read(String id) {
        store.row("SELECT id FROM notifications WHERE id=? AND owner_user_id=?",id,store.owner());
        store.update("UPDATE notifications SET read_at=COALESCE(read_at,?) WHERE id=? AND owner_user_id=?",now(),id,store.owner());
        store.update("UPDATE push_deliveries SET status='cancelled' WHERE notification_id=? AND status='pending'",id);
        return map("ok",true);
    }
    Object configuration() {return map("configured",push.configured(),"publicKey",push.configured()?push.publicKey:"");}
    Object register(Map<String,Object> body,String session) {
        if(!push.configured())throw new ApiException(503,"浏览器推送尚未配置");
        String endpoint=required(body,"endpoint",4096);var keys=object(body.get("keys"));
        String p256dh=required(keys,"p256dh",100),auth=required(keys,"auth",30);
        push.validate(endpoint,p256dh,auth);
        String fingerprint=hash(endpoint);
        var existing=store.rows("SELECT * FROM web_push_subscriptions WHERE endpoint_hash=?",fingerprint);
        String id;
        if(!existing.isEmpty()) {
            var old=existing.get(0);id=text(old.get("id"));
            if(store.owner().equals(old.get("owner_user_id"))&&hash(session).equals(old.get("session_hash"))&&p256dh.equals(old.get("p256dh"))&&auth.equals(old.get("auth"))&&truth(old.get("enabled")))return map("id",id,"ok",true);
            store.update("UPDATE push_deliveries SET status='cancelled' WHERE subscription_id=? AND status='pending'",id);
            store.update("DELETE FROM web_push_subscriptions WHERE id=?",id);
        } else id=id("wps");
        if(number(store.row("SELECT COUNT(*) n FROM web_push_subscriptions WHERE owner_user_id=? AND enabled=TRUE",store.owner()).get("n"))>=20)throw new ApiException(429,"浏览器推送设备过多，请先关闭旧设备通知");
        store.update("INSERT INTO web_push_subscriptions(id,owner_user_id,session_hash,endpoint,endpoint_hash,p256dh,auth,updated_at) VALUES(?,?,?,?,?,?,?,?)",id,store.owner(),hash(session),endpoint,fingerprint,p256dh,auth,now());
        return map("id",id,"ok",true);
    }
    Object unregister(Map<String,Object> body) {
        String endpoint=required(body,"endpoint",4096);
        var found=store.rows("SELECT id FROM web_push_subscriptions WHERE endpoint_hash=? AND owner_user_id=?",hash(endpoint),store.owner());
        if(!found.isEmpty()) {
            String id=text(found.get(0).get("id"));
            store.update("UPDATE web_push_subscriptions SET enabled=FALSE WHERE id=?",id);
            store.update("UPDATE push_deliveries SET status='cancelled' WHERE subscription_id=? AND status='pending'",id);
        }
        return map("ok",true);
    }
    void completed(Map<String,Object> job) {
        if(!"prompt".equals(job.get("kind"))||!TERMINAL.contains(job.get("status"))||truth(object(job.get("payload")).get("steered")))return;
        String status=text(job.get("status"));
        String title=status.equals("succeeded")?"任务已完成":status.equals("failed")?"任务执行失败":"任务已停止";
        String name=job.get("agent_name")==null?"Agent":text(job.get("agent_name"));
        issue(title,name+" 的任务已结束，点开查看详情。",text(job.get("agent_id")),text(job.get("id")),"task",hash("task:"+job.get("id")));
    }
    void approval(Map<String,Object> approval,String jobId) {
        if(!"pending".equals(approval.get("decision")))return;
        var job=store.job(jobId);
        issue("需要你的审批","Agent 正在等待授权，点开会话查看操作详情。",text(job.get("agent_id")),jobId,"approval",hash("approval:"+approval.get("id")));
    }
}
