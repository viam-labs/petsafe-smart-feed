#!/usr/bin/env bash
# meta.json wires setup.sh as the `first_run` hook, so on the happy
# path viam-server runs setup.sh once, installs deps, and this script
# just execs the module server. But first_run only fires once per
# module version — if it fails partway (network flake, PyPI hiccup,
# missing ARM wheel for a transitive dep) the venv is left behind
# incomplete, viam-server doesn't retry the hook, and every subsequent
# start hits "No module named 'src'" until a new version ships.
#
# Do a cheap import check here as a safety net. If the module doesn't
# import (missing venv, broken venv, missing package), run setup.sh
# again before exec'ing. setup.sh's pip install is idempotent.
set -e
cd "$(dirname "$0")"

if [ ! -x ./venv/bin/python ] || ! ./venv/bin/python -c "import src.main" 2>/dev/null; then
    ./setup.sh
fi

exec ./venv/bin/python -m src.main "$@"
