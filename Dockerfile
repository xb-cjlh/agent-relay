FROM maven:3.9.11-eclipse-temurin-17 AS build
WORKDIR /app
COPY server /app/server
COPY relayctl/static /app/relayctl/static
COPY relayctl/skills /app/relayctl/skills
COPY public /app/public
COPY docs /app/docs
RUN mvn -B -f server/pom.xml -DskipTests package

FROM eclipse-temurin:17-jre-jammy
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 relay \
    && mkdir /data && chown relay:relay /data
WORKDIR /app
COPY --from=build /app/server/target/relay-server.jar /app/relay-server.jar
USER relay
ENV RELAY_HOST=0.0.0.0 RELAY_PORT=8765 RELAY_DATA=/data
EXPOSE 8765
VOLUME ["/data"]
ENTRYPOINT ["java", "-jar", "/app/relay-server.jar"]
