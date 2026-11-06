# Minimal production image for Destroyer.

FROM python:3.12.16-alpine3.24

LABEL org.opencontainers.image.title="Destroyer"
LABEL org.opencontainers.image.description="Restart Kubernetes pods in response to security alerts"
LABEL org.opencontainers.image.licenses="Unlicense"
LABEL org.opencontainers.image.source="https://github.com/zinkozapper/Destroyer"

RUN addgroup -g 1000 destroyer && \
    adduser -u 1000 -G destroyer -s /bin/sh -D destroyer

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1

COPY Destroyer.py .
RUN chown -R destroyer:destroyer /app
USER destroyer
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8080/healthz || exit 1

ENTRYPOINT ["python", "-u", "Destroyer.py"]
