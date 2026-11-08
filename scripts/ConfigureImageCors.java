package dev.relay;

import com.aliyun.oss.*;
import com.aliyun.oss.model.*;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.core.env.*;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.util.*;

/** Explicit maintenance: preserve bucket rules and configure only Relay's exact origin. */
public class ConfigureImageCors {
    public static void main(String[] args) throws Exception {
        if(args.length!=2&&args.length!=3)throw new IllegalArgumentException("Use CONFIG --check or CONFIG --apply NEW_BACKUP.json");
        boolean apply=args[1].equals("--apply");
        if(apply&&args.length!=3||!apply&&!args[1].equals("--check"))throw new IllegalArgumentException("Invalid arguments");
        Properties properties=new Properties();
        try(var in=Files.newBufferedReader(Path.of(args[0]),StandardCharsets.UTF_8)){properties.load(in);}
        StandardEnvironment env=new StandardEnvironment();env.getPropertySources().addFirst(new PropertiesPropertySource("relay-oss",properties));
        OssImages storage=new OssImages(env);
        try {
            OSS client=storage.client();CORSConfiguration previous;
            try{previous=client.getBucketCORS(new GenericRequest(storage.bucket));}
            catch(OSSException e){if(!e.getErrorCode().equals("NoSuchCORSConfiguration"))throw e;previous=new CORSConfiguration();previous.setCorsRules(new ArrayList<>());}
            String origin=properties.getProperty("relay.auth.public-origin","https://relay.chaojibot.com");
            var uri=java.net.URI.create(origin);
            if(!uri.getScheme().equals("https")||uri.getHost()==null||uri.getUserInfo()!=null||uri.getQuery()!=null||uri.getFragment()!=null||!Set.of("", "/").contains(uri.getPath()))throw new IllegalArgumentException("Expected exact HTTPS origin");
            List<SetBucketCORSRequest.CORSRule> rules=new ArrayList<>(previous.getCorsRules());
            var methods=List.of("PUT","GET","HEAD");
            var headers=List.of("content-type","content-md5","x-oss-forbid-overwrite","x-oss-meta-upload-id","x-oss-meta-sha256","x-oss-meta-content-md5","x-oss-meta-declared-size");
            boolean configured=rules.stream().anyMatch(rule->rule.getAllowedOrigins().contains(origin)&&rule.getAllowedMethods().containsAll(methods)&&rule.getAllowedHeaders().containsAll(headers));
            if(!apply){System.out.println("Relay CORS configured="+configured+"; existing rules="+rules.size());return;}
            Path backup=Path.of(args[2]);
            Files.write(backup,new ObjectMapper().writeValueAsBytes(previous),StandardOpenOption.CREATE_NEW);Store.privateFile(backup);
            if(!configured) {
                var rule=rules.stream().filter(old->old.getAllowedOrigins().equals(List.of(origin))&&old.getAllowedHeaders().containsAll(headers)).findFirst().orElse(null);
                if(rule==null) {
                    if(rules.size()>=10)throw new IllegalStateException("Bucket CORS rule limit reached; no configuration changed");
                    rule=new SetBucketCORSRequest.CORSRule();rule.setAllowedOrigins(List.of(origin));rule.setAllowedHeaders(headers);rules.add(rule);
                }
                var allowed=new LinkedHashSet<>(rule.getAllowedMethods()==null?List.<String>of():rule.getAllowedMethods());allowed.addAll(methods);rule.setAllowedMethods(new ArrayList<>(allowed));
                rule.setExposeHeaders(List.of("ETag","x-oss-request-id"));rule.setMaxAgeSeconds(600);
                var request=new SetBucketCORSRequest(storage.bucket);request.setCorsRules(rules);request.setResponseVary(previous.getResponseVary());client.setBucketCORS(request);
            }
            System.out.println("Relay CORS ready; original rules preserved; backup="+backup);
        }finally{storage.close();}
    }
}
