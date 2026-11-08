package dev.relay;

import jakarta.servlet.http.*;
import org.springframework.core.env.Environment;
import org.springframework.http.*;
import org.springframework.mail.SimpleMailMessage;
import org.springframework.mail.javamail.JavaMailSenderImpl;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Component;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.*;
import static dev.relay.Values.*;

/** Relay owns its identities and secrets; only SMTP transport may be shared. */
@Component
final class AuthService {
    static final String COOKIE="relay_session";
    final Store store;
    final Environment env;
    final BCryptPasswordEncoder encoder=new BCryptPasswordEncoder(10);
    final String dummy=encoder.encode("relay-invalid-password-placeholder");
    final SecureRandom random=new SecureRandom();
    record Identity(String userId,String keyId) { }
    AuthService(Store store,Environment env) { this.store=store;this.env=env; }
    String cookie(HttpServletRequest request) {
        if(request.getCookies()!=null) for(Cookie c:request.getCookies()) if(COOKIE.equals(c.getName())) return c.getValue();
        return "";
    }
    Identity authenticate(HttpServletRequest request) {
        String header=request.getHeader("Authorization");
        if(header!=null) {
            String raw=header.startsWith("Bearer ")?header.substring(7):"";
            if(!raw.matches("sk-relay_[A-Za-z0-9_-]{43}"))return null;
            var found=store.rows("SELECT id,user_id FROM agent_keys WHERE token_hash=? AND expires_at>? AND revoked_at IS NULL",hash(raw),now());
            if(found.isEmpty())return null;
            var key=found.get(0);
            store.update("UPDATE agent_keys SET last_used_at=? WHERE id=? AND (last_used_at IS NULL OR last_used_at<?)",now(),key.get("id"),now()-60);
            return new Identity(text(key.get("user_id")),text(key.get("id")));
        }
        String raw=cookie(request);
        if(!raw.matches("[A-Za-z0-9_-]{43}"))return null;
        var found=store.rows("SELECT user_id FROM auth_sessions WHERE token_hash=? AND expires_at>?",hash(raw),now());
        if(found.isEmpty())return null;
        String user=text(found.get(0).get("user_id"));
        return new Identity(user,null);
    }
    Object view(Identity identity) {
        if(identity==null)return map("authenticated",false);
        var user=store.row("SELECT id,email,display_name FROM app_users WHERE id=?",identity.userId());
        return map("authenticated",true,"user",user,"authentication",identity.keyId()!=null?"sk":"session");
    }
    void csrf(HttpServletRequest request) {
        if(Set.of("GET","HEAD").contains(request.getMethod())||request.getHeader("Authorization")!=null)return;
        String expected=env.getProperty("relay.auth.public-origin",request.getScheme()+"://"+request.getHeader("Host"));
        String origin=request.getHeader("Origin");
        if(!"1".equals(request.getHeader("X-Relay-Request"))||(origin!=null&&!origin.equals(expected))||"cross-site".equals(request.getHeader("Sec-Fetch-Site")))
            throw new ApiException(403,"不允许跨站提交请求，请刷新页面重试");
    }
    void rate(String scope,int maximum,double seconds) {
        // A separate committed transaction retains attempts even when authentication fails.
        try(Store.Transaction tx=store.transaction()) {
            String key=hash(scope);double time=now();
            store.update("INSERT IGNORE INTO auth_rate_limits(scope_hash,window_started,attempts) VALUES(?,?,0)",key,time);
            var row=store.row("SELECT * FROM auth_rate_limits WHERE scope_hash=? FOR UPDATE",key);
            int count=(int)number(row.get("attempts"));double start=number(row.get("window_started"));
            if(time-start>=seconds) {count=0;start=time;}
            store.update("UPDATE auth_rate_limits SET window_started=?,attempts=? WHERE scope_hash=?",start,count+1,key);
            tx.commit();if(count>=maximum)throw new ApiException(429,"操作过于频繁，请稍后再试");
        } catch(java.sql.SQLException e) {throw new IllegalStateException(e);}
    }
    String email(Object value) {
        String email=text(value).trim().toLowerCase(Locale.ROOT);
        if(email.length()>254||!email.matches("[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")) throw new ApiException("请输入有效邮箱地址");
        return email;
    }
    String password(Object value) {
        String p=text(value);int size=p.getBytes(StandardCharsets.UTF_8).length;
        if(p.length()<8||size>72||p.indexOf('\0')>=0)throw new ApiException("密码至少 8 个字符，最多 72 个 UTF-8 字节");
        return p;
    }
    void browser(Identity identity,HttpServletRequest request) {
        if(identity==null)throw new ApiException(401,"请先登录");
        if(identity.keyId()!=null||request.getHeader("Authorization")!=null)throw new ApiException(403,"请在网页登录后管理账号和 SK");
    }
    ResponseEntity<?> handle(HttpServletRequest request,String path,Map<String,Object> body) throws Exception {
        String method=request.getMethod();
        csrf(request);
        if(method.equals("POST")&&Set.of("/api/auth/email-code","/api/auth/register","/api/auth/login","/api/auth/reset-password").contains(path)) {
            String ip=request.getRemoteAddr();
            rate(path+":ip:"+ip,path.endsWith("login")?60:20,900);
            if(body.containsKey("email"))rate(path+":email:"+email(body.get("email")),path.endsWith("email-code")?5:10,3600);
        }
        try(Store.Transaction tx=store.transaction()) {
            Identity identity=authenticate(request);Object result;String session=null;
            if(method.equals("GET")&&path.equals("/api/auth/session")) result=view(identity);
            else if(method.equals("POST")&&path.equals("/api/auth/email-code")) {sendCode(body);result=map("message","如该邮箱可用于此操作，验证码将发送至邮箱","retryAfter",60);}
            else if(method.equals("POST")&&path.equals("/api/auth/register")) {
                String email=email(body.get("email")), password=password(body.get("password"));
                String name=required(map("name",body.getOrDefault("displayName",email.substring(0,email.indexOf('@')))),"name",40);
                String failure=verify(email,"register",body.get("code"));
                if(failure!=null){tx.commit();throw new ApiException(failure);}
                if(!store.rows("SELECT id FROM app_users WHERE email=?",email).isEmpty())throw new ApiException("邮箱已注册，请登录或重置密码");
                String user=id("usr");
                store.update("INSERT INTO app_users(id,email,display_name,password_hash,created_at) VALUES(?,?,?,?,?)",user,email,name,encoder.encode(password),now());
                store.update("DELETE FROM auth_email_codes WHERE email=? AND purpose='register'",email);
                session=issue(user);result=view(new Identity(user,null));
            } else if(method.equals("POST")&&path.equals("/api/auth/login")) {
                String email=email(body.get("email")), password=text(body.get("password"));
                var users=store.rows("SELECT id,password_hash FROM app_users WHERE email=?",email);
                String hash=users.isEmpty()?dummy:text(users.get(0).get("password_hash"));
                if(password.getBytes(StandardCharsets.UTF_8).length>72||!encoder.matches(password,hash)||users.isEmpty())throw new ApiException(401,"邮箱或密码错误");
                String user=text(users.get(0).get("id"));session=issue(user);result=view(new Identity(user,null));
            } else if(method.equals("POST")&&path.equals("/api/auth/reset-password")) {
                String email=email(body.get("email")), password=password(body.get("password"));
                String failure=verify(email,"reset",body.get("code"));
                if(failure!=null){tx.commit();throw new ApiException(failure);}
                var users=store.rows("SELECT id FROM app_users WHERE email=?",email);
                if(users.isEmpty()){tx.commit();throw new ApiException("验证码错误或已过期");}
                Object user=users.get(0).get("id");
                store.update("UPDATE app_users SET password_hash=? WHERE id=?",encoder.encode(password),user);
                store.update("DELETE FROM auth_sessions WHERE user_id=?",user);
                store.update("UPDATE agent_keys SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",now(),user);
                store.update("DELETE FROM auth_email_codes WHERE email=? AND purpose='reset'",email);
                result=map("message","密码已更新，请重新登录；旧会话与 SK 已失效");
            } else if(method.equals("POST")&&path.equals("/api/auth/logout")) {
                store.update("DELETE FROM auth_sessions WHERE token_hash=?",hash(cookie(request)));session="";result=map("ok",true);
            } else if(path.equals("/api/agent/keys")||path.matches("/api/agent/keys/[a-zA-Z0-9_]+/revoke")) {
                browser(identity,request);result=keys(method,path,identity.userId(),body);
            } else throw new ApiException(404,"接口不存在");
            tx.commit();
            ResponseEntity.BodyBuilder response=ResponseEntity.ok();
            if(session!=null)response.header(HttpHeaders.SET_COOKIE,ResponseCookie.from(COOKIE,session).httpOnly(true).secure(request.isSecure()||env.getProperty("relay.auth.secure-cookie",Boolean.class,false)).sameSite("Lax").path("/").maxAge(session.isEmpty()?0:604800).build().toString());
            return response.body(result);
        }
    }
    String issue(String user) {
        String raw=token();store.update("DELETE FROM auth_sessions WHERE expires_at<=?",now());
        store.update("INSERT INTO auth_sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",hash(raw),user,now()+604800);return raw;
    }
    String verify(String email,String purpose,Object value) {
        String code=text(value);
        if(!code.matches("[0-9]{6}"))return "请输入 6 位邮箱验证码";
        var found=store.rows("SELECT * FROM auth_email_codes WHERE email=? AND purpose=? FOR UPDATE",email,purpose);
        if(found.isEmpty())return "验证码错误或已过期，请重新获取";
        var row=found.get(0);
        if(number(row.get("expires_at"))<=now()||number(row.get("attempts"))>=5)return "验证码错误或已过期，请重新获取";
        store.update("UPDATE auth_email_codes SET attempts=attempts+1 WHERE email=? AND purpose=?",email,purpose);
        return encoder.matches(code,text(row.get("code_hash")))?null:"验证码错误或已过期，请重新获取";
    }
    void sendCode(Map<String,Object> body) {
        String email=email(body.get("email")), purpose=required(body,"purpose",16);
        if(!Set.of("register","reset").contains(purpose))throw new ApiException("验证码用途无效");
        var previous=store.rows("SELECT sent_at FROM auth_email_codes WHERE email=? AND purpose=?",email,purpose);
        if(!previous.isEmpty()&&now()-number(previous.get(0).get("sent_at"))<60)throw new ApiException(429,"请等待 60 秒后重新获取验证码");
        boolean exists=!store.rows("SELECT id FROM app_users WHERE email=?",email).isEmpty();
        String code=String.format(Locale.ROOT,"%06d",random.nextInt(1000000));
        if(purpose.equals("register")?!exists:exists)sendMail(email,purpose,code);
        store.update("INSERT INTO auth_email_codes(email,purpose,code_hash,expires_at,sent_at,attempts) VALUES(?,?,?,?,?,0) ON DUPLICATE KEY UPDATE code_hash=VALUES(code_hash),expires_at=VALUES(expires_at),sent_at=VALUES(sent_at),attempts=0",email,purpose,encoder.encode(code),now()+600,now());
    }
    void sendMail(String email,String purpose,String code) {
        String host=env.getProperty("spring.mail.host",""),from=env.getProperty("relay.mail.from","");
        if(host.isBlank()||from.isBlank())throw new ApiException(503,"验证邮件暂不可用，请配置邮件服务");
        JavaMailSenderImpl sender=new JavaMailSenderImpl();sender.setHost(host);sender.setPort(env.getProperty("spring.mail.port",Integer.class,587));
        sender.setUsername(env.getProperty("spring.mail.username",""));sender.setPassword(env.getProperty("spring.mail.password",""));sender.setDefaultEncoding("UTF-8");
        var props=sender.getJavaMailProperties();
        for(String key:List.of("auth","starttls.enable","starttls.required","ssl.enable"))props.put("mail.smtp."+key,env.getProperty("spring.mail.properties.mail.smtp."+key,key.equals("ssl.enable")?"false":"true"));
        props.put("mail.smtp.ssl.checkserveridentity","true");
        for(String key:List.of("connectiontimeout","timeout","writetimeout"))props.put("mail.smtp."+key,"8000");
        SimpleMailMessage mail=new SimpleMailMessage();mail.setFrom(from);mail.setTo(email);mail.setSubject("Relay · "+(purpose.equals("register")?"注册账号":"重置密码")+"验证码");
        mail.setText("验证码为："+code+"\n\n10 分钟内有效，仅可使用一次。请勿向他人提供验证码。如果这不是你的操作，请忽略此邮件。");
        try{sender.send(mail);}catch(org.springframework.mail.MailException e){throw new ApiException(503,"验证邮件发送失败，请稍后重试");}
    }
    Object keys(String method,String path,String user,Map<String,Object> body) {
        if(method.equals("GET")&&path.equals("/api/agent/keys"))return map("keys",store.rows("SELECT id,name,token_prefix AS prefix,created_at,expires_at,last_used_at,revoked_at FROM agent_keys WHERE user_id=? ORDER BY created_at DESC,id",user));
        if(!method.equals("POST"))throw new ApiException(405,"不支持此方法");
        if(path.endsWith("/revoke")) {
            String id=path.split("/")[4];store.row("SELECT id FROM agent_keys WHERE id=? AND user_id=?",id,user);
            store.update("UPDATE agent_keys SET revoked_at=COALESCE(revoked_at,?) WHERE id=? AND user_id=?",now(),id,user);return map("ok",true);
        }
        String name=required(body,"name",60);
        int days=integer(body.getOrDefault("expiresInDays",90),1,365,"expiresInDays");
        if(number(store.row("SELECT COUNT(*) n FROM agent_keys WHERE user_id=? AND revoked_at IS NULL AND expires_at>?",user,now()).get("n"))>=20)throw new ApiException(409,"最多保留 20 个有效 SK，请先撤销旧密钥");
        String raw="sk-relay_"+token(),id=id("key"),prefix=raw.substring(0,17);double time=now(),expires=time+days*86400;
        store.update("INSERT INTO agent_keys(id,user_id,name,token_hash,token_prefix,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",id,user,name,hash(raw),prefix,time,expires);
        return map("id",id,"name",name,"secret",raw,"prefix",prefix,"expires_at",expires);
    }
}
