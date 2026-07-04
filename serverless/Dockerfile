# Serverless worker image: pdf2md's full routing pipeline (derotate ->
# classify -> text-or-MinerU) in ONE container, wrapped in a RunPod
# Serverless handler.
#
# Why one image instead of the repo's normal one-container-per-engine
# layout: pdf2md-auto.sh orchestrates engines via `docker run`, which isn't
# available inside a serverless worker (no docker-in-docker). So this image
# layers the text engine's small CPU deps on top of the big MinerU image
# and the handler calls each stage directly as a subprocess -- same
# scripts, same routing decisions, no orchestration shell.
#
# Build (from the repo root -- the build context must span both engine dirs):
#   docker build -t pdf2md-serverless -f serverless/Dockerfile .
#
# The base bakes all MinerU model weights in at build time -- deliberate for
# serverless: N fresh workers must start warm, not each cold-download
# several GB of weights on first job (that silently turns a short paid run
# into a long one).
FROM pdf2md-mineru

# The text engine's system deps: tesseract for OSD-based derotation.
# LibreOffice (legacy .doc/.xls) is deliberately NOT included -- the
# serverless path handles pdf/docx/xlsx only; route legacy formats through
# the normal local pipeline, they're rare.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Text engine python deps + the RunPod serverless SDK.
RUN pip install --no-cache-dir pymupdf4llm pytesseract python-docx openpyxl runpod

# The text engine's scripts, verbatim from engines/text -- same source of
# truth as the per-engine images, nothing forked.
COPY engines/text/pdf2md.py /usr/local/bin/pdf2md.py
COPY engines/text/common.py /usr/local/bin/common.py
COPY engines/text/docx2md.py /usr/local/bin/docx2md.py
COPY engines/text/xlsx2md.py /usr/local/bin/xlsx2md.py
# mineru2md.py is already in the base image at /usr/local/bin/mineru2md.py.

COPY serverless/handler.py /usr/local/bin/handler.py

WORKDIR /work
# Override the base's mineru2md.py entrypoint: this image starts the RunPod
# worker loop instead, and the handler invokes mineru2md.py itself when a
# document routes to the MinerU engine.
ENTRYPOINT ["python3", "-u", "/usr/local/bin/handler.py"]
