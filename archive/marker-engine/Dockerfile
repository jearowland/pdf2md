FROM python:3.11-slim-bookworm

# System libraries Marker/Surya (vision models) and PyMuPDF need at runtime.
# tesseract-ocr is used ONLY for its orientation-and-script-detection (OSD) mode:
# a fast, purely geometric per-page rotation check (no text recognition, no content
# decisions) that lets --derotate correct the PDF's /Rotate flag before conversion.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        fonts-dejavu-core \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Send every model/weight cache to /models so a mounted volume persists the
# multi-GB Surya/Marker + HuggingFace downloads across runs (download once).
ENV HOME=/models \
    HF_HOME=/models/huggingface \
    TORCH_HOME=/models/torch \
    XDG_CACHE_HOME=/models/.cache \
    PYTHONUNBUFFERED=1

# marker-pdf pulls a CUDA-enabled torch (used at runtime via --gpus all).
# pymupdf4llm is the fast CPU path for digital PDFs.
# pytesseract is a thin binding onto the tesseract-ocr binary above (OSD only).
RUN pip install --no-cache-dir marker-pdf pymupdf4llm pytesseract

WORKDIR /work
COPY pdf2md.py /usr/local/bin/pdf2md.py
ENTRYPOINT ["python", "/usr/local/bin/pdf2md.py"]
