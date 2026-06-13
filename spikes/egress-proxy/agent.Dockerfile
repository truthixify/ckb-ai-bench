# Spike (NOT production): minimal agent image with curl BAKED IN.
# In production the fat agent image (ADR-0004) bakes its tools at image-build time; the
# agent then runs on the internal-only network with no package-repo access. This mirrors
# that: tools are present before the network is cut, so the isolation test is honest.
FROM alpine:3
RUN apk add --no-cache curl
CMD ["sleep", "600"]
