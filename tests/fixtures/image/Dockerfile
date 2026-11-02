# The self-test image (.github/workflows/ci.yml's selftest-image job): the
# smallest thing image.yml can build, with a `runtime` stage like the DS
# service images.
FROM busybox:1.38.0@sha256:a46257e06c82ad85242d0d38077d0a52d9896389ca60a1a59d3463db586e5fa4 AS runtime
LABEL org.opencontainers.image.description="Distant-Signal/ci self-test image"
CMD ["echo", "Distant-Signal/ci self-test"]
