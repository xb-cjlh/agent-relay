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
        response.setHeader("Cache-Control","no-store");
        response.setHeader("X-Content-Type-Options","nosniff");
        response.setHeader("Referrer-Policy","no-referrer");
        String oss=storageOrigin.isEmpty()?"":" "+storageOrigin;
        response.setHeader("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:"+oss+"; connect-src 'self'"+oss+"; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
        chain.doFilter(request,response);
    }
}
