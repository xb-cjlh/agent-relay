package dev.relay;

import org.springframework.stereotype.Component;
import org.springframework.scheduling.annotation.Scheduled;
import java.util.*;
import static dev.relay.Values.*;

@Component
final class PushDispatcher {
    final Store store;final WebPushClient push;
    PushDispatcher(Store store,WebPushClient push){this.store=store;this.push=push;}
    @Scheduled(fixedDelayString="${relay.web-push.dispatch-interval-ms:5000}",initialDelayString="${relay.web-push.dispatch-initial-ms:5000}")
    void dispatch() throws Exception {
        List<Map<String,Object>> pending;
        synchronized(store) {
            try(var tx=store.transaction()) {
                store.update("UPDATE push_deliveries p JOIN notifications n ON n.id=p.notification_id SET p.status='cancelled' WHERE p.status='pending' AND (n.read_at IS NOT NULL OR n.created_at<? OR NOT EXISTS(SELECT 1 FROM web_push_subscriptions d JOIN auth_sessions s ON s.token_hash=d.session_hash AND s.user_id=d.owner_user_id AND s.expires_at>? WHERE d.id=p.subscription_id AND d.owner_user_id=n.owner_user_id AND d.enabled=TRUE))",now()-86400,now());
                if(!push.configured()){tx.commit();return;}
                pending=store.rows("SELECT p.notification_id,p.subscription_id,p.attempts,n.created_at,n.owner_user_id,d.endpoint,d.p256dh,d.auth,d.updated_at FROM push_deliveries p JOIN notifications n ON n.id=p.notification_id JOIN web_push_subscriptions d ON d.id=p.subscription_id AND d.owner_user_id=n.owner_user_id WHERE p.status='pending' AND p.next_attempt<=? ORDER BY p.next_attempt LIMIT 20 FOR UPDATE",now());
                for(var delivery:pending)store.update("UPDATE push_deliveries SET attempts=attempts+1,next_attempt=? WHERE notification_id=? AND subscription_id=?",now()+300,delivery.get("notification_id"),delivery.get("subscription_id"));
                tx.commit();
            }
        }
        for(var delivery:pending) {
            synchronized(store) {
                try(var tx=store.transaction()) {
                    boolean active=!store.rows("SELECT 1 FROM web_push_subscriptions d JOIN auth_sessions s ON s.token_hash=d.session_hash AND s.expires_at>? AND s.user_id=d.owner_user_id JOIN push_deliveries p ON p.subscription_id=d.id WHERE p.notification_id=? AND p.status='pending' AND d.id=? AND d.owner_user_id=? AND d.updated_at=? AND d.enabled=TRUE",now(),delivery.get("notification_id"),delivery.get("subscription_id"),delivery.get("owner_user_id"),delivery.get("updated_at")).isEmpty();
                    tx.commit();if(!active)continue;
                }
            }
            var result=push.send(delivery);
            synchronized(store) {
                try(var tx=store.transaction()) {
                    int attempts=(int)number(delivery.get("attempts"))+1;
                    String status=result.success()?"sent":result.retry()&&attempts<8?"pending":"failed";
                    store.update("UPDATE push_deliveries SET status=?,last_error=?,next_attempt=? WHERE notification_id=? AND subscription_id=? AND status='pending'",status,result.reason(),now()+Math.min(3600,30*Math.pow(2,attempts)),delivery.get("notification_id"),delivery.get("subscription_id"));
                    if(result.gone())store.update("UPDATE web_push_subscriptions SET enabled=FALSE WHERE id=? AND updated_at=?",delivery.get("subscription_id"),delivery.get("updated_at"));
                    tx.commit();
                }
            }
        }
    }
}
