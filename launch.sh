#!/bin/sh
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
cd "$SCRIPT_DIR" || exit 1
export GSK_RENDERER=gl
exec /usr/bin/python3 app.py "$@"
