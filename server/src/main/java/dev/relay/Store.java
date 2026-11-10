package dev.relay;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.boot.sql.init.dependency.DependsOnDatabaseInitialization;
import javax.sql.DataSource;
import java.nio.file.*;
import java.nio.file.attribute.PosixFilePermissions;
import java.sql.*;
import java.util.*;
import static dev.relay.Values.*;

@Component
@DependsOnDatabaseInitialization
final class Store {
    final Path directory, files;
    final DataSource datasource;
    private final ThreadLocal<Connection> connections = new ThreadLocal<>();
    final ObjectMapper json;
    private final ThreadLocal<String> owners = new ThreadLocal<>();
    String owner() { String value=owners.get(); if(value==null) throw new IllegalStateException("Missing account scope"); return value; }
    void owner(String value) { owners.set(value); }
    void clearOwner() { owners.remove(); }

    Store(@Value("${relay.data}") String data, ObjectMapper json, DataSource datasource) throws Exception {
        this.json = json;
        this.datasource = datasource;
        directory = Path.of(data).toAbsolutePath().normalize();
        Files.createDirectories(directory, PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rwx------")));
        files = directory.resolve("files"); Files.createDirectories(files, PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rwx------")));

    }

    final class Transaction implements AutoCloseable {
        final Connection connection;
        boolean committed;
        Transaction() throws SQLException {
            if (connections.get() != null) throw new IllegalStateException("Nested transaction");
            connection = datasource.getConnection();
            try {
                connection.setTransactionIsolation(Connection.TRANSACTION_READ_COMMITTED);
                connection.setAutoCommit(false);
                connections.set(connection);
            } catch (SQLException e) { connection.close(); throw e; }
        }
        void commit() throws SQLException { connection.commit(); committed = true; }
        @Override public void close() throws SQLException {
            connections.remove();
            try { if (!committed) connection.rollback(); }
            finally { connection.close(); }
        }
    }
    Transaction transaction() throws SQLException { return new Transaction(); }
    private Connection connection() {
        Connection current = connections.get();
        if (current == null) throw new IllegalStateException("Database access requires a transaction");
        return current;
    }
    boolean healthy() {
        try (Connection c = datasource.getConnection(); Statement s = c.createStatement()) {
            s.setQueryTimeout(3);
            return s.executeQuery("SELECT 1").next();
        } catch (SQLException e) { return false; }
    }

    static void privateFile(Path file) throws java.io.IOException {
        try { Files.setPosixFilePermissions(file, PosixFilePermissions.fromString("rw-------")); }
        catch (UnsupportedOperationException ignored) { }
    }
    String encode(Object value) {
        try { return json.writeValueAsString(value); } catch (JsonProcessingException e) { throw new IllegalStateException(e); }
    }
    Object decode(Object value) {
        try { return json.readValue((String)value, Object.class); } catch (JsonProcessingException e) { throw new IllegalStateException(e); }
    }
    List<Map<String,Object>> rows(String sql, Object... args) {
        try (PreparedStatement stmt = connection().prepareStatement(sql)) {
            for (int i=0; i<args.length; i++) stmt.setObject(i+1, args[i]);
            try (ResultSet rs = stmt.executeQuery()) {
                List<Map<String,Object>> result = new ArrayList<>();
                var meta = rs.getMetaData();
                while (rs.next()) {
                    var row = new LinkedHashMap<String,Object>();
                    for (int i=1; i<=meta.getColumnCount(); i++) row.put(meta.getColumnLabel(i), rs.getObject(i));
                    result.add(row);
                }
                return result;
            }
        } catch (SQLException e) { throw new IllegalStateException(e); }
    }
    Map<String,Object> row(String sql, Object... args) {
        var result = rows(sql, args);
        if (result.isEmpty()) throw new ApiException(404, "找不到指定资源");
        return result.get(0);
    }
    void update(String sql, Object... args) {
        try (PreparedStatement stmt = connection().prepareStatement(sql)) {
            for (int i=0; i<args.length; i++) stmt.setObject(i+1, args[i]);
            stmt.executeUpdate();
        } catch (SQLException e) { throw new IllegalStateException(e); }
    }
    void reconcile() {
        update("UPDATE jobs SET status='failed',error=?,finished_at=? WHERE status IN ('running','cancelling') AND lease_until < ?",
            "设备失联或后台进程退出；任务未自动重试，请先核实远端执行情况", now(), now());
    }
    Map<String,Object> device(String ref) { return row("SELECT * FROM devices WHERE (id=? OR name=?) AND merged_into IS NULL AND owner_user_id=?", ref, ref, owner()); }
    Map<String,Object> agent(String ref) {
        var found = rows("SELECT * FROM agents WHERE (id=? OR (name=? AND merged_into IS NULL)) AND device_id IN (SELECT id FROM devices WHERE owner_user_id=?)", ref, ref, owner());
        if (found.isEmpty()) throw new ApiException(404, "找不到会话");
        if (found.size()!=1) throw new ApiException(409, "会话名称重复，请使用会话 ID");
        var value=found.get(0);
        while (value.get("merged_into")!=null) value=row("SELECT * FROM agents WHERE id=? AND device_id IN (SELECT id FROM devices WHERE owner_user_id=?)",value.get("merged_into"),owner());
        return decorateAgent(value,device(text(value.get("device_id"))));
    }
    Map<String,Object> job(String id) {
        var value = row("SELECT j.*,d.name device_name,a.name agent_name,a.kind agent_kind FROM jobs j JOIN devices d ON d.id=j.device_id LEFT JOIN agents a ON a.id=j.agent_id WHERE j.id=? AND d.owner_user_id=?", id, owner());
        value.put("payload", decode(value.get("payload"))); return value;
    }
    private Path devicePath(String value, Map<String,Object> device) {
        Path path=Path.of(value).normalize();
        if (text(device.get("platform")).startsWith("darwin")) {
            for (String alias : List.of("/var","/tmp","/etc")) {
                Path prefix=Path.of(alias);
                if (path.startsWith(prefix)) return Path.of("/private"+alias).resolve(prefix.relativize(path));
            }
        }
        return path;
    }
    private boolean allowedCwd(Object cwd, Map<String,Object> device) {
        return cwd instanceof String value && devicePath(value,device).isAbsolute();
    }
    private Map<String,Object> decorateAgent(Map<String,Object> agent, Map<String,Object> device) {
        Object capabilities=device.get("capabilities");
        var caps=object(capabilities instanceof String ? decode(capabilities) : capabilities);
        if (!allowedCwd(agent.get("cwd"),device)) {
            agent.put("controllable",0); agent.put("control_reason","cwd_missing");
        } else if (!truth(caps.get(text(agent.get("kind"))))) {
            agent.put("controllable",0); agent.put("control_reason","cli_missing");
        } else if ("user".equals(caps.get("access_scope")) && "outside_roots".equals(agent.get("control_reason"))) {
            // Upgraded devices may retain sessions whose transcript is no longer scanned.
            // A former directory restriction must not prevent resuming those sessions.
            agent.put("controllable",1); agent.put("control_reason","");
        }
        return agent;
    }
    private List<Map<String,Object>> deviceSummaries() {
        var devices=rows("""
            SELECT d.id,d.name,d.platform,d.roots,d.capabilities,d.processes,d.last_seen,d.machine_id,
             (SELECT COUNT(*) FROM agents a WHERE a.device_id=d.id AND a.merged_into IS NULL) session_count
            FROM devices d WHERE d.merged_into IS NULL AND d.owner_user_id=? ORDER BY d.name
            """,owner());
        for (var device : devices) {
            for (String key : List.of("roots","capabilities","processes")) device.put(key,decode(device.get(key)));
            device.put("online",now()-number(device.get("last_seen"))<15);
        }
        return devices;
    }
    private static final String SESSION_QUERY="""
        SELECT history.*,
         GREATEST(CASE WHEN origin='native' AND GREATEST(last_activity,COALESCE(native_at,0))>0 THEN 0 ELSE created_at END,
          last_activity,COALESCE(native_at,0),COALESCE(job_at,0)) activity,
         CASE WHEN job_at IS NOT NULL AND (native_at IS NULL OR job_at>=native_at) THEN job_preview ELSE COALESCE(native_preview,'') END preview
        FROM (
         SELECT a.*,d.name device_name,
          (SELECT LEFT(content,140) FROM messages WHERE agent_id=a.id AND duplicate_of IS NULL ORDER BY created_at DESC,id DESC LIMIT 1) native_preview,
          (SELECT created_at FROM messages WHERE agent_id=a.id AND duplicate_of IS NULL ORDER BY created_at DESC,id DESC LIMIT 1) native_at,
          (SELECT LEFT(JSON_UNQUOTE(JSON_EXTRACT(payload,'$.prompt')),140) FROM jobs WHERE agent_id=a.id ORDER BY created_at DESC,id DESC LIMIT 1) job_preview,
          (SELECT created_at FROM jobs WHERE agent_id=a.id ORDER BY created_at DESC,id DESC LIMIT 1) job_at,
          EXISTS(SELECT 1 FROM jobs WHERE agent_id=a.id AND status IN ('running','cancelling')) relay_busy
         FROM agents a JOIN devices d ON d.id=a.device_id
         WHERE d.merged_into IS NULL AND a.merged_into IS NULL AND d.owner_user_id=?
        ) history
        """;
    private List<Map<String,Object>> decorateSessions(List<Map<String,Object>> agents,List<Map<String,Object>> devices) {
        Map<Object,Map<String,Object>> byId=new HashMap<>();
        for(var device:devices)byId.put(device.get("id"),device);
        for(var agent:agents) {
            decorateAgent(agent,byId.get(agent.get("device_id")));
            agent.put("last_activity",agent.remove("activity"));
            agent.put("busy",truth(agent.remove("relay_busy")) || truth(agent.get("external_busy")));
            for(String key:List.of("native_at","job_at","native_preview","job_preview"))agent.remove(key);
        }
        return agents;
    }
    private record SessionCursor(double time,String id) {}
    private SessionCursor sessionCursor(String value) {
        if(value==null)return null;
        String[] parts=value.split("\\|",-1);
        try {
            double time=Double.parseDouble(parts[0]);
            if(parts.length!=2 || !Double.isFinite(time) || time<0 || !parts[1].matches("agt_[a-zA-Z0-9]+"))throw new NumberFormatException();
            return new SessionCursor(time,parts[1]);
        } catch(NumberFormatException e) { throw new ApiException("会话分页参数无效"); }
    }
    private Map<String,Object> sessionPage(String device,String query,String before,String through,List<Map<String,Object>> devices) {
        if(query==null)query="";
        if(query.length()>200)throw new ApiException("搜索内容不能超过 200 个字符");
        String filter=" WHERE 1=1";
        List<Object> args=new ArrayList<>();args.add(owner());
        if(device!=null && !device.isEmpty()) { filter+=" AND device_id=?";args.add(device); }
        if(!query.isBlank()) {
            filter+=" AND LOWER(CONCAT_WS(' ',name,cwd,kind,IF(kind='claude','Claude Code','Codex'),device_name,preview)) LIKE ? ESCAPE '='";
            args.add("%"+query.strip().toLowerCase(Locale.ROOT).replace("=","==").replace("%","=%").replace("_","=_")+"%");
        }
        var cursor=sessionCursor(before);var end=sessionCursor(through);
        if(cursor!=null && end!=null)throw new ApiException("会话分页参数无效");
        String window=filter;
        List<Object> pageArgs=new ArrayList<>(args);
        if(cursor!=null) {
            window+=" AND (activity<? OR (activity=? AND id>?))";
            pageArgs.addAll(List.of(cursor.time(),cursor.time(),cursor.id()));
        }
        if(end!=null) {
            // Poll the loaded portion of the list without pulling older pages.
            window+=" AND (activity>? OR (activity=? AND id<=?))";
            pageArgs.addAll(List.of(end.time(),end.time(),end.id()));
        }
        String source="("+SESSION_QUERY+") sessions";
        var agents=rows("SELECT * FROM "+source+window+" ORDER BY activity DESC,id"+(end==null ? " LIMIT 31" : ""),pageArgs.toArray());
        if(end!=null && agents.isEmpty())return sessionPage(device,query,null,null,devices);
        boolean more=agents.size()>30 && end==null;
        if(more)agents=new ArrayList<>(agents.subList(0,30));
        String next=null;
        if(!agents.isEmpty()) {
            var last=agents.get(agents.size()-1);
            double time=number(last.get("activity"));String id=text(last.get("id"));
            next=Double.toString(time)+"|"+id;
            if(end!=null) {
                args.addAll(List.of(time,time,id));
                more=!rows("SELECT 1 FROM "+source+filter+" AND (activity<? OR (activity=? AND id>?)) LIMIT 1",args.toArray()).isEmpty();
            }
        }
        return map("agents",decorateSessions(agents,devices),"has_more",more,"next_cursor",more ? next:null);
    }
    Map<String,Object> sessions(String device,String query,String before) {
        return sessionPage(device,query,before,null,deviceSummaries());
    }
    Map<String,Object> snapshot() { return snapshot(false,null,null,null,null); }
    Map<String,Object> snapshot(boolean web,String device,String query,String through,String selected) {
        var devices=deviceSummaries();
        var page=web ? sessionPage(device,query,null,through,devices) : null;
        Object agents=web ? page.get("agents") : decorateSessions(rows("SELECT * FROM ("+SESSION_QUERY+") sessions ORDER BY created_at DESC",owner()),devices);
        var jobs=rows("SELECT j.*,d.name device_name,a.name agent_name,a.kind agent_kind FROM jobs j JOIN devices d ON d.id=j.device_id LEFT JOIN agents a ON a.id=j.agent_id WHERE d.owner_user_id=?"+(web ? " AND j.kind='file_read'" : "")+" ORDER BY j.created_at DESC LIMIT 100",owner());
        for (var j : jobs) j.put("payload",decode(j.get("payload")));
        var result=map("devices",devices,"agents",agents,"jobs",jobs,"artifacts",rows("SELECT f.* FROM artifacts f JOIN jobs j ON j.id=f.job_id JOIN devices d ON d.id=j.device_id WHERE d.owner_user_id=? ORDER BY f.created_at DESC LIMIT 100",owner()),"server_time",now());
        if(web) {
            result.put("has_more",page.get("has_more"));result.put("next_cursor",page.get("next_cursor"));
            var remembered=List.<Map<String,Object>>of();
            if(selected!=null && !selected.isEmpty()) {
                boolean scoped=device!=null && !device.isEmpty();
                remembered=decorateSessions(rows("SELECT * FROM ("+SESSION_QUERY+") sessions WHERE id=?"+(scoped ? " AND device_id=?" : ""),
                    scoped ? new Object[]{owner(),selected,device} : new Object[]{owner(),selected}),devices);
            }
            result.put("selected_agent",remembered.isEmpty() ? null : remembered.get(0));
        }
        return result;
    }
    Map<String,Object> register(Map<String,Object> body) {
        String name = required(body,"name",80);
        var roots = roots(body.getOrDefault("roots",List.of())); var caps = object(body.get("capabilities"));
        String machine=body.get("machine_id")==null ? null : machineId(body.get("machine_id"));
        if (machine!=null && !rows("SELECT id FROM devices WHERE machine_id=? AND owner_user_id=?",machine,owner()).isEmpty()) throw new ApiException(409,"本机已经注册；请保留原身份目录，修改操作目录无需再次注册");
        if (!rows("SELECT 1 FROM devices WHERE name=? AND owner_user_id=?",name,owner()).isEmpty()) throw new ApiException(409,"设备名称已存在；请保留原设备状态目录，或使用新名称");
        String id = id("dev"), secret = token();
        update("INSERT INTO devices(id,name,token_hash,platform,roots,capabilities,processes,last_seen,machine_id,owner_user_id) VALUES(?,?,?,?,?,?,'[]',?,?,?)",id,name,hash(secret),required(body,"platform",100),encode(roots),encode(caps),now(),machine,owner());
        return map("id",id,"token",secret);
    }
    Map<String,Object> createAgent(Map<String,Object> body) {
        var device = device(required(body,"device"));
        String name = required(body,"name",80), kind = required(body,"kind",20), cwd = required(body,"cwd");
        if (!Set.of("codex","claude").contains(kind)) throw new ApiException("kind 必须是 codex 或 claude");
        if (!allowedCwd(cwd,device)) throw new ApiException(409,"会话工作目录必须是绝对路径");
        if (!truth(object(decode(device.get("capabilities"))).get(kind))) throw new ApiException(409,"设备上未检测到 " + kind + " CLI");
        if (!rows("SELECT id FROM agents WHERE device_id=? AND name=?",device.get("id"),name).isEmpty()) throw new ApiException(409,"此设备上已存在同名会话");
        String id = id("agt");
        update("INSERT INTO agents(id,device_id,name,kind,cwd,created_at) VALUES(?,?,?,?,?,?)",id,device.get("id"),name,kind,cwd,now());
        return agent(id);
    }
    Map<String,Object> conversation(String ref, String cursor) {
        var agent = agent(ref); var jobs = rows("SELECT * FROM jobs WHERE agent_id=? ORDER BY created_at,id",agent.get("id"));
        var messages = rows("SELECT * FROM messages WHERE agent_id=? AND duplicate_of IS NULL ORDER BY created_at,id",agent.get("id"));
        var relayMessages = new ArrayList<Map<String,Object>>();
        Map<Object,String> prompts = new HashMap<>();
        List<Object> active = new ArrayList<>();
        for (var j : jobs) {
            String id = text(j.get("id")); double created = number(j.get("created_at"));
            var payload=object(decode(j.get("payload")));
            prompts.put(id,text(payload.getOrDefault("prompt","")));
            relayMessages.add(map("id",id+":user","role","user","content",payload.get("prompt"),"images",payload.getOrDefault("images",List.of()),"created_at",created,"job_id",id,"status",j.get("status"),"error",j.get("error")));
            relayMessages.add(map("id",id+":assistant","role","assistant","content",j.get("output"),"diagnostics",j.get("diagnostics"),"created_at",created+0.000001,"job_id",id,"status",j.get("status"),"error",j.get("error")));
            if (!TERMINAL.contains(j.get("status"))) active.add(map("id",id,"status",j.get("status")));
        }
        // Native sync and task streaming can arrive at different times. Overlapping
        // execution times alone do not prove that a native message is represented.
        var suppressed = new ArrayList<String>();
        double current = now();
        messages.removeIf(m -> {
            String content = text(m.get("content")).strip();
            double created = number(m.get("created_at"));
            boolean copied = !content.isEmpty() && jobs.stream().anyMatch(j -> j.get("started_at") != null
                && number(j.get("started_at")) <= created && created <= (j.get("finished_at")==null ? current : number(j.get("finished_at")))
                && ("user".equals(m.get("role")) ? prompts.get(j.get("id")).strip().equals(content)
                    : ("\n"+text(j.get("output")).strip()+"\n").contains("\n"+content+"\n")));
            if(copied)suppressed.add(text(m.get("id")));
            return copied;
        });
        messages.addAll(relayMessages);
        messages.sort(Comparator.<Map<String,Object>>comparingDouble(m -> number(m.get("created_at"))).thenComparing(m -> text(m.get("id"))));
        if (cursor!=null) {
            String[] parts = cursor.split("\\|",2); double before;
            try { before = Double.parseDouble(parts[0]); if (!Double.isFinite(before)) throw new NumberFormatException(); } catch (NumberFormatException e) { throw new ApiException("分页参数无效"); }
            String beforeId = parts.length==2 ? parts[1] : "";
            messages.removeIf(m -> number(m.get("created_at")) > before || (number(m.get("created_at")) == before && text(m.get("id")).compareTo(beforeId)>=0));
        }
        var approvals=rows("SELECT p.* FROM job_approvals p JOIN jobs j ON j.id=p.job_id WHERE j.agent_id=? AND p.decision='pending' AND j.status='running' ORDER BY p.created_at LIMIT 32",agent.get("id"));
        for(var approval:approvals)approval.put("details",decode(approval.get("details")));
        return map("agent",agent,"messages",messages.subList(Math.max(0,messages.size()-100),messages.size()),"has_more",messages.size()>100,"active_jobs",active,"approvals",approvals,"suppressed_message_ids",suppressed);
    }
    void syncSessions(String deviceId, Object value) {
        for (Object item : list(value,100)) {
            var session = object(item); String kind = required(session,"kind",20), sid = sessionId(session.get("session_id")), cwd = required(session,"cwd");
            if (!Set.of("codex","claude").contains(kind)) throw new ApiException("会话类型无效");
            var found = rows("SELECT * FROM agents WHERE device_id=? AND kind=? AND session_id=? AND merged_into IS NULL",deviceId,kind,sid);
            String agentId;
            if (found.isEmpty()) {
                agentId = "agt_"+hash(deviceId+kind+sid).substring(0,20);
                String name = required(session,"name",80)+" · "+sid.substring(Math.max(0,sid.length()-6));
                update("INSERT INTO agents(id,device_id,name,kind,cwd,session_id,created_at,origin) VALUES(?,?,?,?,?,?,?,'native')",agentId,deviceId,name,kind,cwd,sid,now());
            } else agentId = text(found.get(0).get("id"));
            String messageNamespace=found.isEmpty() || found.get(0).get("message_namespace")==null ? deviceId : text(found.get(0).get("message_namespace"));
            Set<String> namespaces=new LinkedHashSet<>(); namespaces.add(messageNamespace);
            for (var alias : rows("SELECT COALESCE(message_namespace,device_id) namespace FROM agents WHERE merged_into=?",agentId)) namespaces.add(text(alias.get("namespace")));
            double updated = number(session.getOrDefault("updated_at",now()));
            Object control=session.getOrDefault("controllable",true);
            if (!(control instanceof Boolean)) throw new ApiException("会话操作权限格式无效");
            String reason=text(session.getOrDefault("control_reason",""));
            if (!Set.of("","outside_roots","cwd_missing","cli_missing").contains(reason)) throw new ApiException("会话状态无效");
            update("UPDATE agents SET external_busy=?,last_activity=?,cwd=?,controllable=?,control_reason=? WHERE id=?",truth(session.get("busy")) ? 1:0,updated,cwd,truth(control) ? 1:0,reason,agentId);
            for (Object entry : list(session.getOrDefault("messages",List.of()),200)) {
                var m = object(entry); String id = required(m,"id",100), role = required(m,"role",20), content = required(m,"content",MAX_OUTPUT);
                if (!Set.of("user","assistant").contains(role)) throw new ApiException("消息角色无效");
                String messageId=hash(messageNamespace+":"+id);
                if (namespaces.size()>1) {
                    List<Object> args=new ArrayList<>(); args.add(agentId);
                    for (String namespace : namespaces) args.add(hash(namespace+":"+id));
                    var copies=rows("SELECT id FROM messages WHERE agent_id=? AND id IN ("+String.join(",",Collections.nCopies(namespaces.size(),"?"))+") ORDER BY id",args.toArray());
                    if (!copies.isEmpty()) {
                        String preferred=messageId;
                        messageId=text(copies.stream().filter(c -> c.get("id").equals(preferred)).findFirst().orElse(copies.get(0)).get("id"));
                        for (var copy : copies) if (!copy.get("id").equals(messageId)) update("UPDATE messages SET duplicate_of=? WHERE id=?",messageId,copy.get("id"));
                    }
                }
                update("INSERT INTO messages(id,agent_id,role,content,created_at) VALUES(?,?,?,?,?) ON DUPLICATE KEY UPDATE content=VALUES(content),duplicate_of=NULL",messageId,agentId,role,content,number(m.getOrDefault("created_at",updated)));
            }
        }
    }
    Map<String,Object> createJob(Map<String,Object> body,List<Object> attachments) {
        String kind = required(body,"kind",20), agentId = null, deviceId; Map<String,Object> payload;
        if (kind.equals("prompt")) {
            var a = agent(required(body,"agent"));
            if (!truth(a.get("controllable"))) throw new ApiException(409,"此会话当前无法执行：工作目录不可用或缺少对应 CLI");
            deviceId = text(a.get("device_id")); agentId = text(a.get("id"));
            String prompt=text(body.getOrDefault("prompt",""));
            if(prompt.isBlank()&&!attachments.isEmpty())prompt="请查看这些图片。";
            int timeout=integer(body.getOrDefault("timeout",0),0,1800,"timeout");
            var caps=object(decode(device(deviceId).get("capabilities")));
            if(timeout==0&&!truth(caps.get("unlimited_tasks")))throw new ApiException(409,"目标设备仍有旧版执行时限，请升级 Relay CLI 至 0.6.1 并重启后台进程");
            payload = map("prompt",required(map("prompt",prompt),"prompt",32000),"timeout",timeout);
            String mode=required(map("mode",body.getOrDefault("permission_mode","device")),"mode",20);
            if(!Set.of("device","ask","full").contains(mode))throw new ApiException("Agent 权限模式无效");
            if(!mode.equals("device")) {
                if(!truth(caps.get(a.get("kind")+"_permissions")))throw new ApiException(409,"请升级目标设备 Relay CLI 至 0.6.0 并重启后台进程");
                if(!truth(caps.get("writable")))throw new ApiException(403,"请先在目标设备启用 --allow-agent-writes");
                payload.put("permission_mode",mode);
            }
            if(!attachments.isEmpty())payload.put("images",attachments);
        } else if (kind.equals("file_read")) {
            var d=device(required(body,"device"));
            deviceId=text(d.get("id")); payload=map("path",required(body,"path"));
        }
        else throw new ApiException("未知任务类型");
        String id = id("job"); update("INSERT INTO jobs(id,device_id,agent_id,kind,payload,status,output,created_at) VALUES(?,?,?,?,?,'queued','',?)",id,deviceId,agentId,kind,encode(payload),now());
        for(Object value:attachments)storeImageJob(object(value),id);
        return job(id);
    }
    Map<String,Object> approval(String jobId,String approvalId,String deviceId) {
        var job=job(jobId);
        if(deviceId!=null&&!deviceId.equals(job.get("device_id")))throw new ApiException(404,"找不到审批请求");
        var result=row("SELECT * FROM job_approvals WHERE id=? AND job_id=?",approvalId,jobId);
        result.put("details",decode(result.get("details")));
        if(!"running".equals(job.get("status")))result.put("decision","expired");
        return result;
    }
    Map<String,Object> requestApproval(String deviceId,String jobId,Map<String,Object> body) {
        var job=job(jobId);
        if(!deviceId.equals(job.get("device_id")))throw new ApiException(404,"找不到任务");
        if(!"running".equals(job.get("status")))throw new ApiException(409,"任务已停止，审批已失效");
        if(!"ask".equals(object(job.get("payload")).get("permission_mode")))throw new ApiException(409,"任务未启用按需审批");
        String id=required(body,"id",64),method=required(body,"method",80);
        if(!id.matches("apr_[0-9a-f]{48}"))throw new ApiException("审批 ID 无效");
        var methods="claude".equals(job.get("agent_kind")) ? Set.of("claude/tool/requestApproval") : Set.of("item/commandExecution/requestApproval","item/fileChange/requestApproval","item/permissions/requestApproval");
        if(!methods.contains(method))throw new ApiException("不支持的审批类型");
        var details=object(body.get("details"));String encoded=encode(details);
        if(encoded.length()>64000)throw new ApiException(413,"审批内容过长，未授权执行");
        var existing=rows("SELECT job_id,method,details FROM job_approvals WHERE id=?",id);
        if(!existing.isEmpty()) {
            var previous=existing.get(0);
            if(!jobId.equals(previous.get("job_id"))||!method.equals(previous.get("method"))||!details.equals(object(decode(previous.get("details")))))throw new ApiException(409,"审批 ID 已被使用");
        } else {
            if(number(row("SELECT COUNT(*) n FROM job_approvals WHERE job_id=?",jobId).get("n"))>=200)throw new ApiException(409,"本轮审批请求过多");
            update("INSERT INTO job_approvals(id,job_id,method,details,created_at) VALUES(?,?,?,?,?)",id,jobId,method,encoded,now());
        }
        return approval(jobId,id,deviceId);
    }
    Map<String,Object> decideApproval(String jobId,String approvalId,Map<String,Object> body) {
        var current=approval(jobId,approvalId,null);
        String decision=required(body,"decision",16);
        if(!Set.of("accept","decline").contains(decision))throw new ApiException("审批决定无效");
        if(!Set.of("pending",decision).contains(text(current.get("decision"))))throw new ApiException(409,"审批已处理或已失效");
        update("UPDATE job_approvals SET decision=?,decided_at=? WHERE id=? AND decision='pending'",decision,now(),approvalId);
        return approval(jobId,approvalId,null);
    }
    Map<String,Object> expireApproval(String jobId,String approvalId,String deviceId) {
        approval(jobId,approvalId,deviceId);
        update("UPDATE job_approvals SET decision='expired',decided_at=? WHERE id=? AND decision='pending'",now(),approvalId);
        return map("ok",true);
    }
    void storeImageJob(Map<String,Object> image,String job) {update("UPDATE images SET job_id=? WHERE id=? AND owner_user_id=? AND job_id IS NULL",job,image.get("id"),owner());}
    Map<String,Object> poll(String deviceId, Map<String,Object> body) {
        if (body.get("machine_id")!=null) {
            String machine=machineId(body.get("machine_id"));
            var current=device(deviceId);
            if (current.get("machine_id")!=null && !machine.equals(current.get("machine_id"))) throw new ApiException(409,"设备身份属于另一台机器");
            if (!rows("SELECT id FROM devices WHERE machine_id=? AND id<>? AND owner_user_id=?",machine,deviceId,owner()).isEmpty()) throw new ApiException(409,"本机已由另一身份注册，请先合并旧设备记录");
            update("UPDATE devices SET machine_id=? WHERE id=?",machine,deviceId);
        }
        var active = list(body.getOrDefault("active",List.of()),16);
        for (Object entry : active) {
            var item = object(entry);
            if (item.get("session_id")!=null) update("UPDATE agents SET session_id=? WHERE id IN (SELECT agent_id FROM jobs WHERE id=? AND device_id=? AND status IN ('running','cancelling'))",sessionId(item.get("session_id")),item.get("id"),deviceId);
        }
        syncSessions(deviceId,body.getOrDefault("sessions",List.of()));
        var processes = list(body.getOrDefault("processes",List.of()),500);
        update("UPDATE devices SET last_seen=?,processes=? WHERE id=?",now(),encode(processes),deviceId);
        if (body.containsKey("roots") || body.containsKey("capabilities")) update("UPDATE devices SET roots=?,capabilities=? WHERE id=?",encode(roots(body.getOrDefault("roots",List.of()))),encode(object(body.get("capabilities"))),deviceId);
        for (Object entry : active) {
            var item = object(entry);
            update("UPDATE jobs SET output=?,diagnostics=?,lease_until=? WHERE id=? AND device_id=? AND status IN ('running','cancelling')",tail(text(item.getOrDefault("output","")),MAX_OUTPUT),tail(text(item.getOrDefault("diagnostics","")),8000),now()+60,item.get("id"),deviceId);
        }
        var cancel = new ArrayList<Object>(); for (var j : rows("SELECT id FROM jobs WHERE device_id=? AND status='cancelling'",deviceId)) cancel.add(j.get("id"));
        int capacity = integer(body.getOrDefault("capacity",0),0,8,"capacity");
        var running = rows("SELECT id,agent_id FROM jobs WHERE device_id=? AND status IN ('running','cancelling')",deviceId);
        int slots = Math.min(capacity, Math.max(0,8-running.size())); Set<Object> busy = new HashSet<>(); for (var j : running) busy.add(j.get("agent_id"));
        var claimed = new ArrayList<Object>();
        for (var j : rows("SELECT * FROM jobs WHERE device_id=? AND status='queued' ORDER BY created_at,id FOR UPDATE",deviceId)) {
            if (claimed.size()>=slots) break;
            if("prompt".equals(j.get("kind"))&&number(object(decode(j.get("payload"))).getOrDefault("timeout",0))==0&&!truth(object(decode(device(deviceId).get("capabilities"))).get("unlimited_tasks"))) {
                update("UPDATE jobs SET status='failed',error=?,finished_at=? WHERE id=?","目标设备不支持无固定时限任务，请升级 Relay CLI 至 0.6.1 并重启后台进程；本次指令未执行",now(),j.get("id"));
                continue;
            }
            String agentId = (String) j.get("agent_id");
            if (agentId!=null) {
                var a=agent(agentId);
                if (!truth(a.get("controllable"))) {
                    update("UPDATE jobs SET status='failed',error=?,finished_at=? WHERE id=?","工作目录不可用或缺少对应 CLI",now(),j.get("id"));
                    continue;
                }
                if (busy.contains(agentId) || truth(a.get("external_busy"))) continue;
            }
            busy.add(agentId); String id = text(j.get("id"));
            update("UPDATE jobs SET status='running',started_at=?,lease_until=? WHERE id=?",now(),now()+60,id);
            var result = job(id); if (agentId!=null) result.put("agent",agent(agentId)); claimed.add(result);
        }
        return map("jobs",claimed,"cancel",cancel);
    }
    Map<String,Object> complete(String deviceId, String jobId, Map<String,Object> body) throws java.io.IOException {
        var j = row("SELECT * FROM jobs WHERE id=? AND device_id=?",jobId,deviceId);
        if (TERMINAL.contains(j.get("status"))) return job(jobId);
        if (!Set.of("running","cancelling").contains(j.get("status"))) throw new ApiException(409,"任务尚未领取");
        String status = text(body.get("status")); if (!TERMINAL.contains(status)) throw new ApiException("完成状态无效");
        if (j.get("status").equals("cancelling")) status="cancelled";
        String sid = body.get("session_id")==null ? null : sessionId(body.get("session_id"));
        String output = tail(text(body.getOrDefault("output","")),MAX_OUTPUT);
        Object error = body.get("error"); if (error!=null) error = head(text(error),4000);
        String artifactId = null;
        if (status.equals("succeeded") && j.get("kind").equals("file_read")) {
            var file = object(body.get("file")); String name = required(file,"name",255);
            if (name.contains("/") || name.contains("\\") || name.equals(".") || name.equals("..") || name.chars().anyMatch(c->c<32)) throw new ApiException("文件名无效");
            byte[] content;
            try { content = Base64.getDecoder().decode(text(file.get("data"))); } catch (IllegalArgumentException e) { throw new ApiException("文件编码无效"); }
            if (content.length>MAX_FILE) throw new ApiException(413,"文件超过 16 MiB 限制");
            String sha = hash(content); if (!secureEquals(sha,text(file.get("sha256")))) throw new ApiException("文件校验失败");
            artifactId = id("file"); Path target = files.resolve(artifactId);
            Files.write(target,content,StandardOpenOption.CREATE_NEW); privateFile(target);
            update("INSERT INTO artifacts VALUES(?,?,?,?,?,?)",artifactId,jobId,name,content.length,sha,now());
        }
        if (j.get("agent_id")!=null) {
            if (sid!=null) update("UPDATE agents SET session_id=? WHERE id=?",sid,j.get("agent_id"));
            update("UPDATE agents SET external_busy=0 WHERE id=?",j.get("agent_id"));
        }
        update("UPDATE jobs SET status=?,output=?,diagnostics=?,error=?,finished_at=?,artifact_id=? WHERE id=?",status,output,tail(text(body.getOrDefault("diagnostics","")),8000),error,now(),artifactId,jobId);
        return job(jobId);
    }
    Map<String,Object> cancel(String id) {
        var j=job(id);
        if (j.get("status").equals("queued")) update("UPDATE jobs SET status='cancelled',finished_at=? WHERE id=?",now(),id);
        else if (j.get("status").equals("running")) update("UPDATE jobs SET status='cancelling' WHERE id=?",id);
        return job(id);
    }
    Map<String,Object> mergeDevices(Map<String,Object> body) {
        var target=device(required(body,"target")); String targetId=text(target.get("id"));
        var refs=list(body.get("sources"),50);
        if (refs.isEmpty()) throw new ApiException("至少需要一个旧设备");
        List<Map<String,Object>> sources=new ArrayList<>(); Set<Object> seen=new HashSet<>();
        for (Object ref : refs) {
            var source=row("SELECT * FROM devices WHERE (id=? OR name=?) AND owner_user_id=?",text(ref),text(ref),owner());
            if (source.get("id").equals(targetId)) throw new ApiException("目标设备不能合并到自身");
            if (!seen.add(source.get("id"))) continue;
            if (source.get("merged_into")!=null) {
                if (source.get("merged_into").equals(targetId)) continue;
                throw new ApiException(409,"旧设备已合并到另一设备");
            }
            if (source.get("machine_id")!=null && !Objects.equals(source.get("machine_id"),target.get("machine_id"))) throw new ApiException(409,"不能合并不同机器的设备身份");
            if (!rows("SELECT id FROM jobs WHERE device_id=? AND status IN ('queued','running','cancelling') LIMIT 1",source.get("id")).isEmpty()) throw new ApiException(409,"请先完成或取消旧设备的任务");
            sources.add(source);
        }
        List<Object> plan=new ArrayList<>();
        Set<List<Object>> nativeSessions=new HashSet<>();
        for (var agent : rows("SELECT kind,session_id FROM agents WHERE device_id=? AND session_id IS NOT NULL AND merged_into IS NULL",targetId)) nativeSessions.add(List.of(agent.get("kind"),agent.get("session_id")));
        for (var source : sources) {
            var agents=rows("SELECT kind,session_id FROM agents WHERE device_id=? AND merged_into IS NULL",source.get("id"));
            int coalesced=0;
            for (var a : agents) if (a.get("session_id")!=null && !nativeSessions.add(List.of(a.get("kind"),a.get("session_id")))) coalesced++;
            plan.add(map("id",source.get("id"),"name",source.get("name"),"agents",agents.size(),"coalesced_sessions",coalesced));
        }
        boolean dryRun=Boolean.TRUE.equals(body.get("dry_run"));
        if (!dryRun) for (var source : sources) {
            for (var sourceAgent : rows("SELECT * FROM agents WHERE device_id=? ORDER BY merged_into IS NOT NULL,created_at",source.get("id"))) {
                var agent=sourceAgent;
                String name=text(agent.get("name"));
                if (!rows("SELECT id FROM agents WHERE device_id=? AND name=?",targetId,name).isEmpty()) name=head(name,90)+" · "+text(agent.get("id"));
                if (agent.get("merged_into")!=null) {
                    update("UPDATE agents SET merged_into=? WHERE id=?",agent(text(agent.get("merged_into"))).get("id"),agent.get("id"));
                } else if (agent.get("session_id")!=null) {
                    var existing=rows("SELECT id FROM agents WHERE device_id=? AND kind=? AND session_id=? AND merged_into IS NULL",targetId,agent.get("kind"),agent.get("session_id"));
                    if (!existing.isEmpty()) {
                        Object canonical=existing.get(0).get("id");
                        update("UPDATE jobs SET agent_id=? WHERE agent_id=?",canonical,agent.get("id"));
                        update("UPDATE messages SET agent_id=? WHERE agent_id=?",canonical,agent.get("id"));
                        update("UPDATE agents SET merged_into=? WHERE merged_into=?",canonical,agent.get("id"));
                        update("UPDATE agents SET merged_into=? WHERE id=?",canonical,agent.get("id"));
                    }
                }
                update("UPDATE agents SET message_namespace=COALESCE(message_namespace,device_id),device_id=?,name=? WHERE id=?",targetId,name,agent.get("id"));
            }
            update("UPDATE devices SET merged_into=?,machine_id=NULL WHERE id=?",targetId,source.get("id"));
        }
        return map("target",map("id",targetId,"name",target.get("name")),"sources",plan,"dry_run",dryRun);
    }

}
