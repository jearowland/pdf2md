# Sourced by pdf2md's scripts before `docker run`. Sets DOCKER_USER for the run's user mapping.
# Rootful Docker (the GPU workers): run as the invoking user, so output files stay theirs.
# Rootless Docker: the container's root already IS the invoking user, while --user would
# map to an unrelated subordinate uid that can't write the output folder. So no --user there.
if docker info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless; then
  DOCKER_USER=()
else
  DOCKER_USER=(--user "$(id -u):$(id -g)")
fi
# DOCKER_HARDEN (security review F-03, 2026-09-27): the text container reads documents uploaded from outside, so
# it gets no network (it needs none: layout model, Tesseract and LibreOffice are all in the image), no
# capabilities or privilege escalation, and pids/memory limits. Accepted by rootful and rootless Docker alike.
DOCKER_HARDEN=(--network none --cap-drop ALL --security-opt no-new-privileges --pids-limit 1024 --memory 12g)
