package dev.relay;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.*;
import org.springframework.web.bind.annotation.*;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.*;
import static dev.relay.Values.*;

@RestController
final class ApiController {
    private final Store store;
    private final PublicContent content;
    private final AuthService auth;
    private final ImageStore images;
    ApiController(Store store, PublicContent content,AuthService auth,ImageStore images) { this.store=store; this.content=content;this.auth=auth;this.images=images; }

    @RequestMapping("/**")
    ResponseEntity<?> handle(HttpServletRequest request) throws Exception {
        String path=request.getRequestURI(), method=request.getMethod();
        if (method.equals("GET") && path.equals("/healthz")) { boolean ok=store.healthy(); return ResponseEntity.status(ok ? 200 : 503).body(map("ok",ok)); }
        if (!path.startsWith("/api/")) {
            if (!method.equals("GET") && !method.equals("HEAD")) throw new ApiException(405,"不支持此方法");
            return content.get(path,method.equals("HEAD"));
        }
        synchronized (store) {
          Map<String,Object> body=Map.of();
          if(method.equals("POST")) {
            if(request.getContentLengthLong()>MAX_BODY)throw new ApiException(413,"请求体超过大小限制");
            byte[] bytes=request.getInputStream().readNBytes(MAX_BODY+1);
            if(bytes.length==0||bytes.length>MAX_BODY)throw new ApiException(413,"请求体为空或超过大小限制");
            try{body=object(store.json.readValue(bytes,Object.class));}catch(com.fasterxml.jackson.core.JsonProcessingException e){throw new ApiException("JSON 格式无效");}
          }
          if(path.startsWith("/api/auth/")||path.equals("/api/agent/keys")||path.startsWith("/api/agent/keys/")) return auth.handle(request,path,body);
          auth.csrf(request);
          try (Store.Transaction transaction=store.transaction()) {
            String authorization=request.getHeader("Authorization"); String secret=authorization!=null && authorization.startsWith("Bearer ") ? authorization.substring(7) : "";
            var identity=auth.authenticate(request);
            var devices = identity==null && !secret.isEmpty() ? store.rows("SELECT id,owner_user_id FROM devices WHERE token_hash=? AND merged_into IS NULL"+(path.startsWith("/api/device/") ? " FOR UPDATE" : ""),hash(secret)) : List.<Map<String,Object>>of();
            String deviceId=devices.isEmpty()?null:text(devices.get(0).get("id"));
            if (identity==null && deviceId==null) throw new ApiException(401,"请登录，或使用有效的 SK");
            if (path.startsWith("/api/device/")) { if (deviceId==null) throw new ApiException(403,"此接口需要设备令牌"); }
            else if (identity==null) throw new ApiException(403,"此接口需要账号 SK 或网页登录");
            store.owner(identity!=null?identity.userId():text(devices.get(0).get("owner_user_id")));
            store.reconcile();
            var result=route(method,path,request,deviceId,body);
            transaction.commit();
            return result;
          } finally {store.clearOwner();}
        }
    }

    ResponseEntity<?> route(String method,String path,HttpServletRequest request,String deviceId,Map<String,Object> body) throws Exception {
        Object result;
        String cursor=request.getParameter("before");
        if (method.equals("GET") && path.equals("/api/state")) result="web".equals(request.getParameter("view"))
            ? store.snapshot(true,request.getParameter("device"),request.getParameter("q"),request.getParameter("through"),request.getParameter("selected"),
                "directory".equals(request.getParameter("organization")),request.getParameter("directory_device"),request.getParameter("cwd")) : store.snapshot();
        else if (method.equals("GET") && path.equals("/api/agents")) result=store.sessions(request.getParameter("device"),request.getParameter("q"),cursor,request.getParameter("directory_device"),request.getParameter("cwd"));
        else if (method.equals("POST") && path.equals("/api/register")) result=store.register(body);
        else if (method.equals("POST") && path.equals("/api/devices/merge")) result=store.mergeDevices(body);
        else if (method.equals("POST") && path.equals("/api/agents")) result=store.createAgent(body);
        else if (method.equals("GET") && path.matches("/api/agents/[^/]+/messages")) result=store.conversation(path.split("/")[3],cursor);
        else if (method.equals("POST") && path.equals("/api/jobs")) {
            List<Object> attachments=List.of();
            if(body.containsKey("images")) {
                if(!"prompt".equals(body.get("kind")))throw new ApiException("只有会话消息支持图片");
                var agent=store.agent(required(body,"agent"));
                attachments=images.validate(body.get("images"),text(agent.get("device_id")));
            }
            result=store.createJob(body,attachments);
        }
        else if(method.equals("POST")&&path.equals("/api/images/uploads"))result=images.initiate(body);
        else if(method.equals("POST")&&path.matches("/api/images/uploads/upl_[a-zA-Z0-9]+/complete"))result=images.complete(path.split("/")[4]);
        else if(method.equals("GET")&&path.matches("/api/images/img_[a-zA-Z0-9]+"))return images.preview(path.split("/")[3]);
        else if(method.equals("GET")&&path.matches("/api/images/img_[a-zA-Z0-9]+/url"))result=images.previewUrl(path.split("/")[3]);
        else if(method.equals("GET")&&path.matches("/api/device/images/img_[a-zA-Z0-9]+"))return images.download(path.split("/")[4],deviceId);
        else if (method.equals("GET") && path.matches("/api/jobs/[^/]+")) result=store.job(path.split("/")[3]);
        else if (method.equals("POST") && path.matches("/api/jobs/[^/]+/cancel")) result=store.cancel(path.split("/")[3]);
        else if (method.equals("POST") && path.matches("/api/jobs/[^/]+/approvals/[^/]+")) result=store.decideApproval(path.split("/")[3],path.split("/")[5],body);
        else if (method.equals("POST") && path.matches("/api/device/jobs/[^/]+/approvals")) result=store.requestApproval(deviceId,path.split("/")[4],body);
        else if (method.equals("GET") && path.matches("/api/device/jobs/[^/]+/approvals/[^/]+")) result=store.approval(path.split("/")[4],path.split("/")[6],deviceId);
        else if (method.equals("POST") && path.matches("/api/device/jobs/[^/]+/approvals/[^/]+/expire")) result=store.expireApproval(path.split("/")[4],path.split("/")[6],deviceId);
        else if (method.equals("POST") && path.equals("/api/device/poll")) result=store.poll(deviceId,body);
        else if (method.equals("POST") && path.matches("/api/device/jobs/[^/]+/complete")) result=store.complete(deviceId,path.split("/")[4],body);
        else if (method.equals("GET") && path.equals("/api/files/cache")) result=map("file",store.fileStore.cached(required(map("device",request.getParameter("device")),"device"),required(map("path",request.getParameter("path")),"path")));
        else if (method.equals("GET") && path.matches("/api/files/[^/]+/url")) result=store.fileStore.url(path.split("/")[3]);
        else if (method.equals("POST") && path.matches("/api/device/jobs/[^/]+/file-upload")) result=store.fileStore.initiate(deviceId,path.split("/")[4],body);
        else if (method.equals("GET") && path.matches("/api/files/[^/]+")) {
            var file=store.fileStore.artifact(path.split("/")[3]);
            store.job(text(file.get("job_id")));
            String filename=URLEncoder.encode(text(file.get("name")),StandardCharsets.UTF_8).replace("+","%20");
            return ResponseEntity.ok().contentType(MediaType.APPLICATION_OCTET_STREAM).header("X-SHA256",text(file.get("sha256"))).header("Content-Disposition","attachment; filename*=UTF-8''"+filename).body(store.fileStore.read(file));
        } else throw new ApiException(404,"接口不存在");
        return ResponseEntity.ok(result);
    }
    @org.springframework.scheduling.annotation.Scheduled(fixedDelayString="${relay.files.cleanup-interval-ms:3600000}",initialDelayString="${relay.files.cleanup-initial-ms:60000}")
    void cleanFileCache() {
        synchronized(store) {
            try(Store.Transaction transaction=store.transaction()) {
                store.fileStore.cleanup(); transaction.commit();
            } catch(Exception e) { org.slf4j.LoggerFactory.getLogger(getClass()).warn("File cache cleanup postponed"); }
        }
    }
    @ExceptionHandler(ApiException.class)
    ResponseEntity<?> apiError(ApiException e) { return ResponseEntity.status(e.status).body(map("error",e.getMessage())); }
    @ExceptionHandler(Exception.class)
    ResponseEntity<?> internalError(Exception e) {
        org.slf4j.LoggerFactory.getLogger(getClass()).error("Request failed: {}",e.getClass().getSimpleName());
        return ResponseEntity.internalServerError().body(map("error","服务端内部错误"));
    }
}
