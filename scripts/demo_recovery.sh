#!/usr/bin/env sh
set -eu
# demo_core uses a fresh public CLI process for every approval/resume phase.
exec sh "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/demo_core.sh"
