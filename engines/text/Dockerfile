FROM python:3.11-slim-bookworm

# tesseract-ocr is used ONLY for its orientation-and-script-detection (OSD) mode:
# a fast, purely geometric per-page rotation check (no text recognition, no
# content decisions) that lets --derotate correct the PDF's /Rotate flag before
# conversion. No GPU, no heavy model weights -- this container is CPU-only.
#
# libreoffice-writer / libreoffice-calc (headless) handle legacy pre-2007
# binary .doc/.xls input -- confirmed a real case: a document lodged as
# old-style .doc, which has no ZIP-based structure python-docx/openpyxl can
# read at all (only .docx/.xlsx). Rather
# than build a bespoke legacy-binary-format reader, pdf2md-auto.sh converts
# these to PDF first (soffice --headless --convert-to pdf) and then runs the
# ordinary, already-hardened PDF pipeline (derotate/classify/text-or-MinerU)
# on the result -- same approach already proven ad hoc for a legacy .xlsx
# case earlier in this project. --writer/--calc only (not the full suite)
# keeps this reasonably scoped to the two formats actually seen.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libreoffice-writer \
        libreoffice-calc \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

# pymupdf4llm is the fast CPU path for digital PDFs.
# pytesseract is a thin binding onto the tesseract-ocr binary above (OSD only).
# python-docx / openpyxl are pure structural readers for docx2md.py /
# xlsx2md.py -- no OCR, no model weights, same CPU-only profile as the PDF
# path, so they share this image rather than needing one of their own.
RUN pip install --no-cache-dir pymupdf4llm pytesseract python-docx openpyxl

WORKDIR /work
COPY pdf2md.py /usr/local/bin/pdf2md.py
COPY common.py /usr/local/bin/common.py
COPY verify_numbers.py /usr/local/bin/verify_numbers.py
COPY docx2md.py /usr/local/bin/docx2md.py
COPY xlsx2md.py /usr/local/bin/xlsx2md.py
ENTRYPOINT ["python", "/usr/local/bin/pdf2md.py"]
