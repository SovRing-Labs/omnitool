#!/usr/bin/env bash
# ==============================================================================
# SovRing test-suite installer (template from armilla-astrolabe/install.sh; REPO-ACCESS-0 B4)
#   In a kit folder : unpacks the repo from <repo>.bundle, prepares deps, runs the suite headless, opens the page.
#   At a repo root  : uses this checkout + its suite.json; fetches sovring-suite.pyz if it is not beside this file.
# Usage: bash install.sh [--no-gui] [--no-prepare] [--repo NAME]
# Needs: python3 >= 3.8 and bash. git only when unpacking a bundle. Nothing is installed system-wide:
# Python packages go into ~/.sovring/venv (SOVRING_HOME moves it).
# ==============================================================================
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
REPO="tarsus"
PYZ_URL="https://github.com/SovRing-Labs/SovRing/releases/latest/download/sovring-suite.pyz"
GUI=1; PREP=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-gui) GUI=0 ;;
    --no-prepare) PREP=0 ;;
    --repo) REPO="$2"; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
if [ "$REPO" = "tarsus" ] && [ -f suite.json ]; then
  REPO="$(python3 -c 'import json;print(json.load(open("suite.json"))["_about"]["repo"])')"
fi

echo "=================================================================="
echo "    SovRing-Labs · $REPO · test suite"
echo "=================================================================="

echo "[1/4] Checking python3 ..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required (3.8 or newer): https://www.python.org/downloads/" >&2
  exit 1
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || { echo "ERROR: python3 >= 3.8 needed, found $(python3 -V)" >&2; exit 1; }

echo "[2/4] Locating the repo and the runner ..."
if [ -f "$DIR/$REPO.bundle" ]; then
  REPOS="$DIR/repos"
  if [ ! -d "$REPOS/$REPO" ]; then
    command -v git >/dev/null 2>&1 || { echo "ERROR: git is needed to unpack $REPO.bundle" >&2; exit 1; }
    mkdir -p "$REPOS"
    git -c advice.detachedHead=false clone -q "$DIR/$REPO.bundle" "$REPOS/$REPO"
    echo "  unpacked $REPO.bundle → repos/$REPO ($(git -C "$REPOS/$REPO" rev-parse --short HEAD))"
  else
    echo "  using repos/$REPO"
  fi
elif [ -f "$DIR/suite.json" ]; then
  REPOS="$(dirname "$DIR")"
  [ "$(basename "$DIR")" = "$REPO" ] || { REPOS="$DIR/.sovring-repos"; mkdir -p "$REPOS"; ln -sfn "$DIR" "$REPOS/$REPO"; }
  echo "  using this checkout: $DIR"
else
  REPOS="${SOVRING_HOME:-$HOME/.sovring}/repos"
  echo "  no bundle and no suite.json here: the runner will git clone $REPO into $REPOS"
fi
PYZ="$DIR/sovring-suite.pyz"
if [ ! -f "$PYZ" ]; then
  echo "  fetching sovring-suite.pyz from $PYZ_URL"
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$PYZ_URL" -o "$PYZ"
  else python3 -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" "$PYZ_URL" "$PYZ"; fi
fi
R=(python3 "$PYZ" --repos "$REPOS")

echo "[3/4] Running the $REPO suite headless (tests that need hardware or services are listed, not run) ..."
if [ "$PREP" = 1 ]; then "${R[@]}" --prepare "$REPO" || echo "  (prepare failed: tests that need the missing packages will SKIP)"; fi
set +e
"${R[@]}" --run "$REPO"
RC=$?
set -e
[ "$RC" = 0 ] && echo "  suite: no FAIL" || echo "  suite: at least one FAIL (see above; results JSON path printed)"

if [ "$GUI" = 1 ]; then
  echo "[4/4] Opening the suite page (127.0.0.1, Ctrl+C to stop) ..."
  exec "${R[@]}"
fi
exit "$RC"
