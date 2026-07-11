# Hearth — serve-only container
#
# The image contains the ENVIRONMENT only (Python, uv, git, rsync); Hearth's
# code arrives at runtime via the entrypoint (fetch-at-runtime model — see
# CLAUDE-hearth-docker.md). The image rarely needs rebuilding: code updates
# flow through git on container start, per HEARTH_UPDATE.
#
# Future (documented, not built): a `gui` stage layering the Qt system
# libraries from hearth-sys-depends.sh on top of this base, for native-window
# mode via X11 passthrough. Deliberately deferred — serve-only first.

# python:3.12-slim rather than debian-slim + uv-managed Python: it satisfies
# every app's `requires-python = ">=3.11"`, gives the entrypoint a python3
# for JSON-safe hearth.json handling, and (with UV_PYTHON_PREFERENCE below)
# guarantees uv never downloads its own interpreter to a non-persisted path.
FROM python:3.12-slim

# git:   runtime clone (fetch-at-runtime)
# rsync: the installer's sync engine (gitignore-aware code/data separation)
# ca-certificates: https clone of the repo
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        rsync \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv binary from Astral's official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# UV_CACHE_DIR on the /data volume: PEP 723 script environments are built
# inside the cache, so persisting it makes every start after the first
# near-instant. UV_PYTHON_PREFERENCE pins uv to the image's system Python.
# HEARTH_DOCKER=1 tells hearthmonitor it's running in the container — it
# gates the local-machine UI (OPEN buttons, Launchers panel, settings form)
# and guards the matching API endpoints. Environment, not hearth.json, on
# purpose: a runtime fact, not a user preference — it must not travel with
# the data directory if it migrates to a native install.
ENV HEARTH_NONINTERACTIVE=1 \
    HEARTH_INSTALL_DIR=/data \
    HEARTH_DOCKER=1 \
    UV_CACHE_DIR=/data/uv-cache \
    UV_PYTHON_PREFERENCE=only-system

COPY hearth-install.sh /usr/local/bin/hearth-install.sh
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/hearth-install.sh /usr/local/bin/entrypoint.sh \
    && mkdir -p /data

# hearthmonitor's default serve port. Apps started from the monitor bind
# their own ports — publish a range or use host networking (see compose).
EXPOSE 8000

# No VOLUME directive on purpose: /data must be mounted explicitly (compose
# does this). Declaring VOLUME here would litter anonymous volumes on ad-hoc
# `docker run` invocations that forget the mount.

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
