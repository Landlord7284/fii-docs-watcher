# The robot needs no compiler and no system packages: every dependency is a
# pure-Python wheel, httpx verifies TLS against the certifi bundle it carries,
# and sqlite3, expat and zlib are already in the slim image. So there is no
# apt-get here at all -- if one appears, something has been added that the
# project did not need.

FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv && /opt/venv/bin/pip install .


FROM python:3.13-slim

# PYTHONUNBUFFERED so `docker logs` shows a run as it happens rather than when
# it ends -- runs take minutes here, and the difference is between watching
# progress and staring at nothing.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/app \
    FII_WATCHER_CONFIG=/config/config.toml

# The two roots are fixed by the volume layout, so the image pins them rather
# than trusting the mounted file to have been edited. Getting this wrong means
# writing the archive into the container's own filesystem and losing it on the
# next recreate; the host side of the mapping belongs in compose, not here.
ENV FII_WATCHER_PATHS_DATA_ROOT=/data \
    FII_WATCHER_PATHS_DOCUMENTS_ROOT=/documents

COPY --from=builder /opt/venv /opt/venv
COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/scheduler.py /app/scheduler.py

# The default identity when PUID/PGID are not given. The container starts as
# root so the entrypoint can make the two roots writable by whatever uid the
# host asks for, and drops to it before running anything -- see the comment in
# entrypoint.sh for why compose's `user:` cannot do this on its own.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --home-dir /home/app --create-home app \
    && chmod +x /entrypoint.sh \
    && mkdir -p /data /documents /config \
    && chown app:app /data /documents \
    && chmod 0777 /home/app

WORKDIR /home/app

VOLUME ["/data", "/documents"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["scheduler"]
