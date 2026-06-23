# Timefold Solver Project Validator
# Java 17 + Maven environment for compiling and running generated projects

FROM eclipse-temurin:17-jdk-jammy

# Install Maven
RUN apt-get update && \
    apt-get install -y maven && \
    rm -rf /var/lib/apt/lists/*

# Pre-bake Maven dependencies so the first real compile is fast.
# Uses pom-warmup.xml which mirrors the standard generated project's dep set.
COPY pom-warmup.xml /tmp/warmup/pom.xml
RUN mkdir -p /tmp/warmup/src/main/java && \
    echo 'public class Warmup {}' > /tmp/warmup/src/main/java/Warmup.java && \
    cd /tmp/warmup && mvn dependency:go-offline -q || true

# Set working directory
WORKDIR /project

# Default command: compile the project
# Override with docker run ... mvn exec:java for execution
CMD ["mvn", "clean", "compile", "-q"]
