# Production egress proxy image (ADR-0006).
# Baking tinyproxy avoids the chicken-and-egg where an internal-only network blocks apk.
# Promoted from spikes/egress-proxy/proxy.Dockerfile.
FROM alpine:3.21
RUN apk add --no-cache tinyproxy
CMD ["tinyproxy", "-d", "-c", "/conf/tinyproxy.conf"]