package dev.relay;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.web.context.WebServerInitializedEvent;
import org.springframework.context.event.EventListener;

@SpringBootApplication
public class RelayApplication {
    public static void main(String[] args) {
        SpringApplication application=new SpringApplication(RelayApplication.class);
        application.run(args);
    }

    @EventListener
    public void ready(WebServerInitializedEvent event) {
        System.out.println("RELAY_READY " + event.getWebServer().getPort());
    }
}
