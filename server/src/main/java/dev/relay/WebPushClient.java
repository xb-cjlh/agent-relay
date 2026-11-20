package dev.relay;

import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;
import com.fasterxml.jackson.databind.ObjectMapper;
import javax.crypto.*;
import javax.crypto.spec.*;
import java.io.ByteArrayOutputStream;
import java.math.BigInteger;
import java.net.URI;
import java.net.http.*;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.*;
import java.security.interfaces.*;
import java.security.spec.*;
import java.time.Duration;
import java.util.*;
import static dev.relay.Values.*;

/** RFC 8291 aes128gcm payload encryption and RFC 8292 VAPID authentication. */
@Component
final class WebPushClient {
    final String publicKey,subject,testOrigin;
    final PrivateKey privateKey;
    final ObjectMapper json;
    final HttpClient http=HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).followRedirects(HttpClient.Redirect.NEVER).build();
    static final SecureRandom RANDOM=new SecureRandom();
    record Result(boolean success,boolean retry,boolean gone,String reason) {}
    WebPushClient(Environment env,ObjectMapper json) throws Exception {
        this.json=json;subject=env.getProperty("relay.web-push.subject","https://relay.chaojibot.com");
        testOrigin=env.getProperty("relay.web-push.test-origin","");
        if(!testOrigin.isEmpty()&&!Set.of("127.0.0.1","localhost").contains(URI.create(testOrigin).getHost()))throw new IllegalArgumentException("Push test origin must be loopback");
        String file=env.getProperty("relay.web-push.key-file","");
        if(file.isBlank()){privateKey=null;publicKey="";return;}
        var keys=json.readTree(Files.readString(Path.of(file)));
        publicKey=keys.path("publicKey").asText();
        privateKey=KeyFactory.getInstance("EC").generatePrivate(new PKCS8EncodedKeySpec(Base64.getUrlDecoder().decode(keys.path("privateKey").asText())));
        publicPoint(publicKey);
        if(!(subject.startsWith("mailto:")||subject.startsWith("https://")))throw new IllegalArgumentException("Web Push subject requires a mailto or HTTPS contact");
        // Fail early if the configured VAPID pair does not match.
        var signer=Signature.getInstance("SHA256withECDSA");signer.initSign(privateKey);signer.update(new byte[]{1});byte[] proof=signer.sign();
        signer.initVerify(publicPoint(publicKey));signer.update(new byte[]{1});if(!signer.verify(proof))throw new IllegalArgumentException("Web Push key pair mismatch");
    }
    boolean configured(){return privateKey!=null;}
    static String b64(byte[] bytes){return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);}
    static byte[] bytes(String value){return value.getBytes(StandardCharsets.UTF_8);}
    static byte[] join(byte[]... values) {
        var out=new ByteArrayOutputStream();for(var value:values)out.writeBytes(value);return out.toByteArray();
    }
    static KeyPair pair() throws GeneralSecurityException {
        var generator=KeyPairGenerator.getInstance("EC");generator.initialize(new ECGenParameterSpec("secp256r1"));return generator.generateKeyPair();
    }
    static byte[] scalar(BigInteger n) {
        byte[] source=n.toByteArray(),value=new byte[32];int length=Math.min(32,source.length);
        System.arraycopy(source,source.length-length,value,32-length,length);return value;
    }
    static byte[] point(PublicKey key) {
        var p=((ECPublicKey)key).getW();return join(new byte[]{4},scalar(p.getAffineX()),scalar(p.getAffineY()));
    }
    static PublicKey publicPoint(String encoded) throws GeneralSecurityException {
        byte[] raw=Base64.getUrlDecoder().decode(encoded);
        if(raw.length!=65||raw[0]!=4)throw new InvalidKeyException("Invalid P-256 public key");
        var params=AlgorithmParameters.getInstance("EC");params.init(new ECGenParameterSpec("secp256r1"));
        var point=new ECPoint(new BigInteger(1,Arrays.copyOfRange(raw,1,33)),new BigInteger(1,Arrays.copyOfRange(raw,33,65)));
        var spec=params.getParameterSpec(ECParameterSpec.class);
        BigInteger prime=((ECFieldFp)spec.getCurve().getField()).getP(),x=point.getAffineX(),y=point.getAffineY();
        if(x.compareTo(prime)>=0||y.compareTo(prime)>=0||!y.multiply(y).mod(prime).equals(x.pow(3).add(spec.getCurve().getA().multiply(x)).add(spec.getCurve().getB()).mod(prime)))throw new InvalidKeyException("Point is not on P-256");
        return KeyFactory.getInstance("EC").generatePublic(new ECPublicKeySpec(point,spec));
    }
    static byte[] hmac(byte[] key,byte[] data) throws GeneralSecurityException {
        var mac=Mac.getInstance("HmacSHA256");mac.init(new SecretKeySpec(key,"HmacSHA256"));return mac.doFinal(data);
    }
    static byte[] expand(byte[] key,byte[] info,int length) throws GeneralSecurityException {
        return Arrays.copyOf(hmac(key,join(info,new byte[]{1})),length);
    }
    void validate(String endpoint,String p256dh,String auth) {
        try {
            URI uri=URI.create(endpoint);String host=uri.getHost();
            boolean test=!testOrigin.isEmpty()&&(uri.getScheme()+"://"+uri.getRawAuthority()).equals(testOrigin);
            if(!test&&(!"https".equals(uri.getScheme())||uri.getPort()!=-1||!Set.of("web.push.apple.com","fcm.googleapis.com","updates.push.services.mozilla.com").contains(host)))throw new IllegalArgumentException();
            if(uri.getRawUserInfo()!=null||uri.getRawFragment()!=null||uri.getPath()==null||uri.getPath().isEmpty())throw new IllegalArgumentException();
            publicPoint(p256dh);if(Base64.getUrlDecoder().decode(auth).length!=16)throw new IllegalArgumentException();
        } catch(Exception e){throw new ApiException("浏览器推送订阅无效或推送服务不受支持");}
    }
    byte[] encrypt(String p256dh,String auth,byte[] payload) throws GeneralSecurityException {
        var ephemeral=pair();byte[] local=point(ephemeral.getPublic()),remote=Base64.getUrlDecoder().decode(p256dh),salt=new byte[16];RANDOM.nextBytes(salt);
        var agreement=KeyAgreement.getInstance("ECDH");agreement.init(ephemeral.getPrivate());agreement.doPhase(publicPoint(p256dh),true);
        byte[] prkKey=hmac(Base64.getUrlDecoder().decode(auth),agreement.generateSecret());
        byte[] ikm=expand(prkKey,join(bytes("WebPush: info\0"),remote,local),32);
        byte[] prk=hmac(salt,ikm),key=expand(prk,bytes("Content-Encoding: aes128gcm\0"),16),nonce=expand(prk,bytes("Content-Encoding: nonce\0"),12);
        var cipher=Cipher.getInstance("AES/GCM/NoPadding");cipher.init(Cipher.ENCRYPT_MODE,new SecretKeySpec(key,"AES"),new GCMParameterSpec(128,nonce));
        byte[] encrypted=cipher.doFinal(join(payload,new byte[]{2}));
        return join(salt,ByteBuffer.allocate(4).putInt(4096).array(),new byte[]{65},local,encrypted);
    }
    String authorization(URI endpoint) throws Exception {
        long expires=System.currentTimeMillis()/1000+43200;
        String header=b64(bytes("{\"typ\":\"JWT\",\"alg\":\"ES256\"}"));
        String claims=json.writeValueAsString(map("aud",endpoint.getScheme()+"://"+endpoint.getRawAuthority(),"exp",expires,"sub",subject));
        String input=header+"."+b64(bytes(claims));var signer=Signature.getInstance("SHA256withECDSAinP1363Format");signer.initSign(privateKey);signer.update(bytes(input));
        return "vapid t="+input+"."+b64(signer.sign())+", k="+publicKey;
    }
    Result send(Map<String,Object> delivery) {
        try {
            String endpoint=text(delivery.get("endpoint")),key=text(delivery.get("p256dh")),auth=text(delivery.get("auth"));validate(endpoint,key,auth);
            // Only a generic alert is visible on the lock screen; private details require login.
            byte[] payload=bytes(json.writeValueAsString(map("id",delivery.get("notification_id"),"title","Relay","body","你有一条新的 Agent 通知")));
            URI uri=URI.create(endpoint);
            var request=HttpRequest.newBuilder(uri).timeout(Duration.ofSeconds(8)).header("Authorization",authorization(uri))
                .header("Content-Encoding","aes128gcm").header("Content-Type","application/octet-stream").header("TTL","86400").header("Urgency","normal")
                .POST(HttpRequest.BodyPublishers.ofByteArray(encrypt(key,auth,payload))).build();
            int status=http.send(request,HttpResponse.BodyHandlers.discarding()).statusCode();
            return new Result(status==201||status==200||status==202,status==429||status>=500,status==404||status==410,"HTTP_"+status);
        }catch(Exception e){if(e instanceof InterruptedException)Thread.currentThread().interrupt();return new Result(false,true,false,"TransportUnavailable");}
    }
}
