# Spike (NOT production): proxy image with tinyproxy BAKED IN.
# Baking avoids the chicken-and-egg where an internal-only network blocks apk.
FROM alpine:3
RUN apk add --no-cache tinyproxy
# Run in the foreground (-d) reading the mounted config.
CMD ["tinyproxy", "-d", "-c", "/conf/tinyproxy.conf"]
