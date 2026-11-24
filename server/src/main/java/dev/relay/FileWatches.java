package dev.relay;

import java.util.*;
import static dev.relay.Values.*;

/** Short-lived preview subscriptions, accessed under Store's request lock. */
final class FileWatches {
    private record Lease(String owner,String device,String path,double expires) {}
    private final Map<String,Lease> leases=new HashMap<>();
    private final Store store;
    private final FileStore files;
    FileWatches(Store store,FileStore files){this.store=store;this.files=files;}
    private void expire(){leases.values().removeIf(l->l.expires()<=now());}
    Map<String,Object> subscribe(Map<String,Object> body) {
        expire();
        String id=required(body,"id",64),device=text(store.device(required(body,"device")).get("id")),path=required(body,"path",4096);
        if(!id.matches("fw_[a-f0-9]{32}"))throw new ApiException("文件监听 ID 无效");
        var old=leases.get(id);
        if(old!=null&&(!old.owner().equals(store.owner())||!old.device().equals(device)||!old.path().equals(path)))throw new ApiException(404,"文件监听不存在");
        if(truth(body.get("stop"))){leases.remove(id);return map("supported",true);}
        if(!truth(object(store.decode(store.device(device).get("capabilities"))).get("file_watch")))return map("supported",false);
        if(old==null&&(leases.size()>=4096||leases.values().stream().filter(l->l.owner().equals(store.owner())).count()>=64))throw new ApiException(429,"同时预览的文件过多");
        leases.put(id,new Lease(store.owner(),device,path,now()+45));
        return status(id);
    }
    Map<String,Object> status(String id) {
        expire();var lease=leases.get(id);
        if(lease==null||!lease.owner().equals(store.owner()))return null;
        return map("id",id,"supported",true,"expires_at",lease.expires(),"file",files.latestMetadata(lease.device(),lease.path()));
    }
    List<Object> forDevice(String device) {
        expire();var paths=new LinkedHashMap<String,Double>();
        for(var lease:leases.values())if(lease.device().equals(device)&&lease.owner().equals(store.owner()))paths.merge(lease.path(),lease.expires(),Math::max);
        return paths.entrySet().stream().map(e->(Object)map("path",e.getKey(),"ttl",Math.max(0,e.getValue()-now()))).toList();
    }
    Map<String,Object> refresh(String device,Map<String,Object> body) {
        expire();String path=required(body,"path",4096);
        if(leases.values().stream().noneMatch(l->l.device().equals(device)&&l.path().equals(path)&&l.owner().equals(store.owner())))throw new ApiException(410,"文件预览监听已结束");
        var active=store.rows("SELECT * FROM jobs WHERE device_id=? AND kind='file_read' AND status IN ('queued','running','cancelling') AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.path'))=? ORDER BY created_at,id LIMIT 1",device,path);
        if(!active.isEmpty()) {
            var job=active.get(0);
            if(!truth(object(store.decode(job.get("payload"))).get("file_watch")))return map("job",null);
            return map("job",store.job(text(job.get("id"))));
        }
        var job=store.createJob(map("kind","file_read","device",device,"path",path),List.of(),List.of());
        var payload=object(job.get("payload"));payload.put("file_watch",true);
        store.update("UPDATE jobs SET payload=?,status='running',started_at=?,lease_until=? WHERE id=?",store.encode(payload),now(),now()+60,job.get("id"));
        return map("job",store.job(text(job.get("id"))));
    }
}
