#!/bin/sh
# Dispatch between the scheduling loop and a single command.
#
#   (no arguments) / scheduler   the periodic driver, the image's default
#   anything else                passed straight to the CLI, so
#                                `run --dry-run`, `doctor`, `add`, `status`
#                                and the interactive commands all work
#
# Every branch uses `exec` so the final process keeps PID 1 and receives
# SIGTERM directly. Without it the signal would stop at this shell and the
# pipeline would be killed mid-step instead of standing down cleanly.
set -eu

# --- ownership, then drop privileges -----------------------------------------
#
# The archive is meant to be read by whoever mounts the share, so the uid that
# owns it has to be the host's choice rather than the image's. Doing that with
# compose's `user:` alone does not work: a named volume takes its ownership
# from the image at creation, so an arbitrary uid arrives to find /data owned
# by someone else and cannot write the manifest. So the container starts as
# root, makes the two roots match PUID:PGID, and immediately becomes that user
# for the actual work.
if [ "$(id -u)" = "0" ]; then
    PUID="${PUID:-1000}"
    PGID="${PGID:-1000}"

    for root in /data /documents; do
        # Recursive only when it is actually wrong -- on the first run, or
        # after PUID changes. Otherwise this would walk the whole archive on
        # every start.
        if [ "$(stat -c '%u:%g' "$root")" != "$PUID:$PGID" ]; then
            echo "adjusting ownership of $root to $PUID:$PGID" >&2
            chown -R "$PUID:$PGID" "$root"
        fi
    done

    # --clear-groups rather than --init-groups: PUID need not exist in
    # /etc/passwd, and looking it up would fail for exactly the arbitrary uid
    # this exists to support.
    exec setpriv --reuid "$PUID" --regid "$PGID" --clear-groups "$0" "$@"
fi

# --- from here on we are the unprivileged user -------------------------------

: "${FII_WATCHER_CONFIG:=/config/config.toml}"
export FII_WATCHER_CONFIG

# `--version` and `--help` answer from the binary alone. Demanding a config
# file for them would make the two commands someone reaches for first fail on
# an image that is working perfectly.
case "${1:-}" in
    --version | -h | --help)
        exec fii-docs-watcher "$@"
        ;;
esac

if [ ! -f "$FII_WATCHER_CONFIG" ]; then
    # The application would refuse to start anyway -- an explicitly named
    # config file that does not exist is an error, never a fallback -- but it
    # cannot know that the file was supposed to arrive over a mount.
    echo "no configuration at $FII_WATCHER_CONFIG" >&2
    echo "Copy config.example.toml from the repository and mount it there, e.g." >&2
    echo "  volumes:" >&2
    echo "    - ./config.toml:/config/config.toml:ro" >&2
    exit 2
fi

if [ "$#" -eq 0 ] || [ "$1" = "scheduler" ]; then
    exec python /app/scheduler.py
fi

exec fii-docs-watcher "$@"
