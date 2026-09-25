# Sourced by pdf2md's scripts before `docker run`. Sets DOCKER_USER for the run's user mapping.
# Rootful Docker (the GPU workers): run as the invoking user, so output files stay theirs.
# Rootless Docker (core, since 2026-09-26): the container's root already IS the invoking user, while --user would
# map to an unrelated subordinate uid that can't write the output folder. So no --user there.
if docker info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless; then
  DOCKER_USER=()
else
  DOCKER_USER=(--user "$(id -u):$(id -g)")
fi
