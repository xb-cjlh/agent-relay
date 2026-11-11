package dev.relay;

import java.nio.file.*;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.io.ClassPathResource;
import org.springframework.core.io.FileSystemResource;
import org.springframework.http.*;
import org.springframework.stereotype.Component;

@Component
final class PublicContent {
    private static final Set<String> CACHEABLE_ASSETS = Set.of("/app.js","/file-preview.js","/account.js","/image-upload.js","/style.css","/favicon.svg","/docs.js","/docs.css","/vendor/spark-md5.min.js","/vendor/markdown-it.min.js");
    static boolean cacheableAsset(String path) { return CACHEABLE_ASSETS.contains(path); }
    private final Path downloads;
    private final Map<String,String> assetTags = new ConcurrentHashMap<>();
    PublicContent(@Value("${relay.downloads:./dist/downloads}") String downloads) {
        this.downloads=Path.of(downloads).toAbsolutePath().normalize();
    }
    ResponseEntity<?> get(String path, boolean head) throws Exception {
        String file=Map.ofEntries(
            Map.entry("/","static/index.html"), Map.entry("/app.js","static/app.js"),
            Map.entry("/account.js","static/account.js"),
            Map.entry("/file-preview.js","static/file-preview.js"),
            Map.entry("/image-upload.js","static/image-upload.js"),
            Map.entry("/vendor/spark-md5.min.js","static/vendor/spark-md5.min.js"),
            Map.entry("/vendor/spark-md5.LICENSE","static/vendor/spark-md5.LICENSE"),
            Map.entry("/vendor/markdown-it.min.js","static/vendor/markdown-it.min.js"),
            Map.entry("/vendor/markdown-it.LICENSE","static/vendor/markdown-it.LICENSE"),
            Map.entry("/style.css","static/style.css"), Map.entry("/favicon.svg","static/favicon.svg"),
            Map.entry("/docs","static/docs.html"), Map.entry("/docs/","static/docs.html"),
            Map.entry("/docs.js","static/docs.js"), Map.entry("/docs.css","static/docs.css"),
            Map.entry("/docs/install.md","public/install.md"), Map.entry("/install.sh","public/install.sh"),
            Map.entry("/docs/images.md","public/images.md"),
            Map.entry("/install-agent.md","public/install-agent.md"),
            Map.entry("/skills/agent-bridge/SKILL.md","public/agent-bridge/SKILL.md")
        ).get(path);
        if (file!=null) {
            var resource=new ClassPathResource(file);
            String mime=file.endsWith(".html") ? "text/html;charset=UTF-8" : file.endsWith(".css") ? "text/css;charset=UTF-8" : file.endsWith(".js") ? "text/javascript;charset=UTF-8" : file.endsWith(".svg") ? "image/svg+xml" : "text/plain;charset=UTF-8";
            var response=ResponseEntity.ok().contentType(MediaType.parseMediaType(mime)).contentLength(resource.contentLength());
            if (cacheableAsset(path)) {
                String tag=assetTags.get(file);
                if (tag==null) {
                    try (var in=resource.getInputStream()) {
                        tag="W/\""+HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(in.readAllBytes()))+"\"";
                    }
                    assetTags.put(file,tag);
                }
                // Cache public assets, but validate their content on each visit so
                // unversioned URLs immediately pick up a deployed update.
                response.cacheControl(CacheControl.noCache().cachePublic()).eTag(tag);
            }
            return response.body(head ? null : resource);
        }
        if (path.equals("/downloads/current.json") || path.matches("/downloads/[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}/(SHA256SUMS|relayctl-(darwin|linux)-(amd64|arm64))")) {
            Path target=downloads.resolve(path.substring("/downloads/".length())).normalize();
            if (!target.startsWith(downloads) || !Files.isRegularFile(target) || !target.toRealPath().startsWith(downloads.toRealPath())) throw new ApiException(404,"安装包尚未发布");
            String mime=path.endsWith(".json") ? "application/json" : path.endsWith("SHA256SUMS") ? "text/plain;charset=UTF-8" : "application/octet-stream";
            var response=ResponseEntity.ok().contentType(MediaType.parseMediaType(mime)).contentLength(Files.size(target));
            if (mime.equals("application/octet-stream")) response.header("Content-Disposition","attachment; filename=\""+target.getFileName()+"\"");
            return response.body(head ? null : new FileSystemResource(target));
        }
        throw new ApiException(404,"页面不存在");
    }
}
