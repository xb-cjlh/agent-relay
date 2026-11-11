package dev.relay;

import jakarta.servlet.*;
import jakarta.servlet.http.*;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;
import java.io.IOException;

@Component
final class SecurityHeaders extends OncePerRequestFilter {
    private final String storageOrigin;
    SecurityHeaders(OssImages oss){storageOrigin=oss.connectOrigin();}
    @Override protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain) throws ServletException, IOException {
        if (!PublicContent.cacheableAsset(request.getRequestURI()) || !(request.getMethod().equals("GET") || request.getMethod().equals("HEAD")))
            response.setHeader("Cache-Control","no-store");
        response.setHeader("X-Content-Type-Options","nosniff");
        response.setHeader("Referrer-Policy","no-referrer");
        if (request.getRequestURI().equals("/diagram.html")) {
            // WebKit gives sandboxed documents an opaque origin: 'self' cannot
            // load their static assets. Match the request host explicitly; both
            // schemes support deployments behind an HTTPS reverse proxy.
            String host=java.net.URI.create(request.getRequestURL().toString()).getRawAuthority();
            String assets="http://"+host+" https://"+host;
            response.setHeader("Content-Security-Policy","default-src 'none'; script-src "+assets+"; style-src "+assets+" 'unsafe-inline'; img-src data:; connect-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'; sandbox allow-scripts");
            chain.doFilter(request,response); return;
        }
        String oss=storageOrigin.isEmpty()?"":" "+storageOrigin;
        response.setHeader("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:"+oss+"; connect-src 'self'"+oss+"; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
        chain.doFilter(request,response);
    }
}
