package dev.relay;

import com.fasterxml.jackson.core.StreamReadConstraints;
import org.springframework.boot.autoconfigure.jackson.Jackson2ObjectMapperBuilderCustomizer;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
class JsonConfig {
    @Bean
    Jackson2ObjectMapperBuilderCustomizer fileTransferLimits() {
        // A 16 MiB artifact expands to over 22 MiB as base64. The controller
        // independently caps the entire HTTP body before parsing any JSON.
        return builder -> builder.postConfigurer(mapper -> mapper.getFactory().setStreamReadConstraints(
            StreamReadConstraints.builder().maxStringLength(Values.MAX_BODY).build()));
    }
}
