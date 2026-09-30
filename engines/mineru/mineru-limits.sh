# Sourced by mineru.sh and tools/mineru-session: MINERU_LIMITS, the MinerU container's memory cap
# and how much work MinerU keeps in flight, sized from this machine.
#
# At MinerU's defaults (3 concurrent requests, each holding a 64-page window of rendered page images)
# its API grew past a fixed 20 GB cap and was OOM-killed mid-job on a real batch (on a 31 GB and
# a 24 GB machine alike); and several tasks at once deadlocked its layout model on another. So: fewer pages in
# flight (a 16-page window, ONE request at a time -- with one request per document each carries
# many pages, so the GPU still gets full batches; override with PDF2MD_MINERU_WINDOW /
# PDF2MD_MINERU_CONCURRENCY), and a cap of this machine's RAM less 7 GB for
# the host and the text containers, at most 24 GB (PDF2MD_MINERU_MEMORY_MB overrides).
_total_mb=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo)
_cap=$(( _total_mb - 7168 > 24576 ? 24576 : _total_mb - 7168 ))
MINERU_MEMORY_MB="${PDF2MD_MINERU_MEMORY_MB:-$(( _cap < 4096 ? 4096 : _cap ))}"
MINERU_LIMITS=(
  --memory "${MINERU_MEMORY_MB}m"
  -e "MINERU_PROCESSING_WINDOW_SIZE=${PDF2MD_MINERU_WINDOW:-16}"
  -e "MINERU_API_MAX_CONCURRENT_REQUESTS=${PDF2MD_MINERU_CONCURRENCY:-1}"
)
