# Layers a thin PDF->one-markdown wrapper on MinerU's official image.
#
# BUILD THE BASE FIRST (MinerU's maintained image, correct for the vllm hybrid
# backend — don't hand-roll it):
#   wget https://gcore.jsdelivr.net/gh/opendatalab/MinerU@master/docker/global/Dockerfile -O Dockerfile.mineru
#   docker build -t mineru:latest -f Dockerfile.mineru .
# Then build this:
#   docker build -t pdf2md-mineru .
FROM mineru:latest

# The base image (mineru:latest) bakes ALL models in at build time and sets
# MINERU_MODEL_SOURCE=local via its entrypoint. We override the entrypoint below,
# so re-assert local here — this uses the baked-in weights (reproducible, offline,
# no multi-GB runtime download).
ENV MINERU_MODEL_SOURCE=local \
    PYTHONUNBUFFERED=1

# wamerican: a plain wordlist (no ML, ~1MB) used ONLY to detect the pattern
# "a repeated rare token has a same-document real-word variant" -- the signature
# of hybrid-engine's VLM silently autocorrecting an unusual proper noun. It never
# picks a spelling itself; see mineru2md.py's reconcile_spelling().
RUN apt-get update && apt-get install -y --no-install-recommends wamerican \
    && rm -rf /var/lib/apt/lists/*

COPY mineru2md.py /usr/local/bin/mineru2md.py
WORKDIR /work
ENTRYPOINT ["python3", "/usr/local/bin/mineru2md.py"]
