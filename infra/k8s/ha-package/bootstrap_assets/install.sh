#!/bin/sh
# Explicit root entrypoint for a newly provisioned owned guest. No network installer.
set -eu
[ "$#" -eq 2 ] || exit 64
exec python3 "$1/bootstrap.py" install --bundle "$1" --manifest-sha256 "$2"
