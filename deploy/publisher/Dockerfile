FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates skopeo \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "httpx>=0.28,<1"

# The job checks out the repository and runs scripts/publish_image.py; this image
# only supplies Python, httpx and skopeo. Reference it by digest in IMAGE_PUBLISHER_TOOL_IMAGE.
USER 65532:65532
