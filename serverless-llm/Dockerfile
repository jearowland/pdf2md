# Generic serverless LLM worker: ollama runtime in the image, model weights
# on a RunPod network volume (pulled once at first boot, warm for every
# worker after). Job contract: {model, prompt, format (json schema),
# options} -> {response}. Domain-agnostic by design -- prompts and contexts
# arrive per job.
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates zstd python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*
RUN curl -sL https://github.com/ollama/ollama/releases/download/v0.31.1/ollama-linux-amd64.tar.zst \
        -o /tmp/o.tar.zst && \
    tar --zstd -xf /tmp/o.tar.zst -C /usr/local && rm /tmp/o.tar.zst
RUN pip3 install --break-system-packages --no-cache-dir runpod requests
ENV OLLAMA_MODELS=/runpod-volume/ollama
COPY handler.py /handler.py
CMD ["python3", "-u", "/handler.py"]
