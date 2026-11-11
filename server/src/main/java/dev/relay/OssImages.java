package dev.relay;

import com.aliyun.oss.*;
import com.aliyun.oss.model.*;
import com.aliyun.oss.common.auth.DefaultCredentialProvider;
import com.aliyun.oss.common.comm.SignVersion;
import jakarta.annotation.PreDestroy;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;
import java.net.URI;
import java.util.*;

@Component
final class OssImages {
    final String endpoint, uploadEndpoint, region, bucket, accessKey, secret, token, prefix;
    private OSS reader, signer;
    OssImages(Environment env) {
        endpoint=env.getProperty("relay.oss.endpoint","").trim();
        uploadEndpoint=env.getProperty("relay.oss.upload-endpoint",endpoint.replace("-internal.",".")).trim();
        bucket=env.getProperty("relay.oss.bucket","").trim();
        accessKey=env.getProperty("relay.oss.access-key-id","").trim();
        secret=env.getProperty("relay.oss.access-key-secret","").trim();
        token=env.getProperty("relay.oss.security-token","").trim();
        String detected="";
        if(!uploadEndpoint.isEmpty()) {
            var match=java.util.regex.Pattern.compile("^oss-([a-z0-9-]+)\\.aliyuncs\\.com$").matcher(URI.create(uploadEndpoint).getHost());
            if(match.matches())detected=match.group(1);
        }
        region=env.getProperty("relay.oss.region",detected).trim();
        String raw=env.getProperty("relay.oss.prefix","relay-dev/").trim();
        prefix=raw.endsWith("/")?raw:raw+"/";
        if(raw.isEmpty()||raw.startsWith("/")||raw.contains("..")||raw.length()>200||!raw.matches("[a-zA-Z0-9_/-]+"))throw new IllegalArgumentException("relay.oss.prefix 必须为独立的桶内相对前缀");
    }
    boolean configured() { return !endpoint.isEmpty()&&!bucket.isEmpty()&&!accessKey.isEmpty()&&!secret.isEmpty(); }
    synchronized OSS client() {
        if(endpoint.isEmpty()||bucket.isEmpty()||accessKey.isEmpty()||secret.isEmpty())throw new ApiException(503,"图片存储尚未配置");
        if(reader==null)reader=build(endpoint,false);
        return reader;
    }
    private OSS build(String url,boolean v4) {
        ClientBuilderConfiguration config=new ClientBuilderConfiguration();
        config.setConnectionTimeout(5000);config.setSocketTimeout(10000);config.setMaxErrorRetry(1);
        if(v4)config.setSignatureVersion(SignVersion.V4);
        var builder=OSSClientBuilder.create().endpoint(url).clientConfiguration(config)
            .credentialsProvider(token.isEmpty()?new DefaultCredentialProvider(accessKey,secret):new DefaultCredentialProvider(accessKey,secret,token));
        if(v4)builder.region(region);
        return builder.build();
    }
    String connectOrigin() {
        if(uploadEndpoint.isEmpty()||bucket.isEmpty())return "";
        URI uri=URI.create(uploadEndpoint);String host=uri.getHost();
        boolean loopback="127.0.0.1".equals(host)||"localhost".equals(host);
        if(host==null||uri.getUserInfo()!=null||uri.getQuery()!=null||uri.getFragment()!=null||!("https".equals(uri.getScheme())||loopback&&"http".equals(uri.getScheme())))throw new IllegalArgumentException("OSS 上传地址必须使用 HTTPS");
        return uri.getScheme()+"://"+(loopback?host:bucket+"."+host)+(uri.getPort()<0?"":":"+uri.getPort());
    }
    Map<String,Object> presign(String key,int size,String mime,String md5,String sha,String uploadId,long expires) {
        OSS signing=signerClient();
        Map<String,String> headers=new LinkedHashMap<>();
        headers.put("Content-Type",mime);headers.put("Content-MD5",md5);
        headers.put("x-oss-forbid-overwrite","true");headers.put("x-oss-meta-upload-id",uploadId);
        headers.put("x-oss-meta-sha256",sha);headers.put("x-oss-meta-content-md5",md5);headers.put("x-oss-meta-declared-size",Integer.toString(size));
        GeneratePresignedUrlRequest request=new GeneratePresignedUrlRequest(bucket,prefix+key,HttpMethod.PUT);
        request.setExpiration(new Date(expires));request.setContentType(mime);request.setContentMD5(md5);
        request.addHeader("Content-Length",Integer.toString(size));
        request.addAdditionalHeaderName("content-length");request.addAdditionalHeaderName("content-type");request.addAdditionalHeaderName("content-md5");
        headers.forEach((name,value)->{if(name.startsWith("x-oss-"))request.addHeader(name,value);});
        try{return Values.map("url",signing.generatePresignedUrl(request).toString(),"headers",headers);}
        catch(OSSException|ClientException e){throw unavailable();}
    }
    String presignGet(String key,String mime,long expires) {
        if(!Set.of("image/png","image/jpeg").contains(mime))throw new ApiException("图片格式无效");
        OSS signing=signerClient();
        GeneratePresignedUrlRequest request=new GeneratePresignedUrlRequest(bucket,prefix+key,HttpMethod.GET);
        request.setExpiration(new Date(expires));
        ResponseHeaderOverrides response=new ResponseHeaderOverrides();
        response.setCacheControl("private, max-age=300");
        request.setResponseHeaders(response);
        try{return signing.generatePresignedUrl(request).toString();}
        catch(OSSException|ClientException e){throw unavailable();}
    }
    String presignFileGet(String key,String name,long expires) {
        GeneratePresignedUrlRequest request=new GeneratePresignedUrlRequest(bucket,prefix+key,HttpMethod.GET);
        request.setExpiration(new Date(expires));
        ResponseHeaderOverrides response=new ResponseHeaderOverrides();
        response.setCacheControl("private, max-age=300");
        response.setContentDisposition("attachment; filename*=UTF-8''"+java.net.URLEncoder.encode(name,java.nio.charset.StandardCharsets.UTF_8).replace("+","%20"));
        request.setResponseHeaders(response);
        try{return signerClient().generatePresignedUrl(request).toString();}
        catch(OSSException|ClientException e){throw unavailable();}
    }
    void writeFile(String key,byte[] data,String sha) {
        ObjectMetadata metadata=new ObjectMetadata();
        metadata.setContentLength(data.length);metadata.setContentType("application/octet-stream");
        try { metadata.setContentMD5(Base64.getEncoder().encodeToString(java.security.MessageDigest.getInstance("MD5").digest(data))); }
        catch(java.security.NoSuchAlgorithmException e){throw new IllegalStateException(e);}
        metadata.addUserMetadata("sha256",sha);
        try {client().putObject(bucket,prefix+key,new java.io.ByteArrayInputStream(data),metadata);}
        catch(OSSException|ClientException e){throw unavailable();}
    }
    byte[] readFile(String key) {
        try(var object=client().getObject(bucket,prefix+key);var in=object.getObjectContent()) {
            byte[] bytes=in.readNBytes(Values.MAX_FILE+1);
            if(bytes.length>Values.MAX_FILE)throw new ApiException(413,"文件超过 16 MiB 限制");
            return bytes;
        }catch(OSSException|ClientException|java.io.IOException e){throw unavailable();}
    }
    private synchronized OSS signerClient() {
        client();
        if(region.isEmpty())throw new ApiException(503,"图片存储区域尚未配置");
        connectOrigin();
        if(signer==null)signer=build(uploadEndpoint,true);
        return signer;
    }
    ObjectMetadata head(String key) {
        try{return client().getObjectMetadata(bucket,prefix+key);}
        catch(OSSException e){if("NoSuchKey".equals(e.getErrorCode()))return null;throw unavailable();}
        catch(ClientException e){throw unavailable();}
    }
    byte[] read(String key) {
        try(var object=client().getObject(bucket,prefix+key);var in=object.getObjectContent()) {
            byte[] bytes=in.readNBytes(ImageStore.MAX_IMAGE+1);
            if(bytes.length>ImageStore.MAX_IMAGE)throw new ApiException(413,"图片超过大小限制");
            return bytes;
        }catch(OSSException|ClientException|java.io.IOException e){throw unavailable();}
    }
    void delete(String key) {
        try{client().deleteObject(bucket,prefix+key);}catch(OSSException|ClientException e){throw unavailable();}
    }
    private ApiException unavailable(){return new ApiException(503,"图片存储暂不可用，请稍后重试");}
    @PreDestroy synchronized void close(){if(reader!=null)reader.shutdown();if(signer!=null)signer.shutdown();}
}
