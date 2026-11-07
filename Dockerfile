# The demo image: the patch panel replaying the session recorded from a real QNX guest. It needs no keys, no QNX SDP
# and no guest, and it starts nothing but the panel. The drivers and the live guest are built by the scripts in
# scripts/ on a machine with QNX SDP 8.0; this image does not contain them.
#
#   docker build --target demo -t patchbay:demo .
#   docker run --rm -p 127.0.0.1:8093:8093 patchbay:demo        then open http://127.0.0.1:8093/
#
# The back end is standard-library Python, so there is nothing to pip install.

FROM python:3.12-slim-bookworm AS demo
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY patchbay/ patchbay/
RUN useradd --system --no-create-home --uid 10001 patchbay
USER patchbay
EXPOSE 8093
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8093/api/state', timeout=4).read()"]
# 0.0.0.0 is inside the container only; a recorded session has no working fault buttons. Publish it on 127.0.0.1.
CMD ["python", "-m", "patchbay", "panel", "--recorded", "--host", "0.0.0.0", "--port", "8093"]
