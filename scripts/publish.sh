#!/usr/bin/env bash
# Publish the release that is already committed and tagged.
#
#   scripts/publish.sh            # build, upload, then check the index
#   scripts/publish.sh --dry-run  # build and upload nothing
#
# The token comes from $PYPI_TOKEN, or from ~/.config/luna-doctor/pypi-token.
# A project-scoped token is enough: it publishes to this project and no other.
#
# Nothing here cuts a release. Run `scripts/release.sh <bump>` first, commit,
# tag, and push, so the uploaded files match a tag that exists in public.

set -euo pipefail

cd "$(dirname "$0")/.."

dry_run=0
[ "${1:-}" = "--dry-run" ] && dry_run=1

version="$(tr -d '[:space:]' < VERSION)"
tag="v$version"

if [ -n "$(git status --porcelain -- . ':!dist')" ]; then
  echo "working tree is dirty; the uploaded files must match the tag" >&2
  git status --porcelain >&2
  exit 1
fi

if ! git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  echo "no local tag $tag; run scripts/release.sh and tag the release first" >&2
  exit 1
fi

token="${PYPI_TOKEN:-}"
if [ -z "$token" ]; then
  token_file="${PYPI_TOKEN_FILE:-$HOME/.config/luna-doctor/pypi-token}"
  if [ ! -f "$token_file" ]; then
    echo "no PyPI token: set PYPI_TOKEN or write $token_file" >&2
    exit 1
  fi
  token="$(tr -d '[:space:]' < "$token_file")"
fi

if [ -z "$token" ]; then
  echo "the PyPI token is empty" >&2
  exit 1
fi

echo "release: $version ($tag)"

rm -rf dist
uv build

echo
echo "built:"
ls -1 dist

if [ "$dry_run" = 1 ]; then
  echo
  echo "dry run: nothing uploaded"
  UV_PUBLISH_TOKEN="$token" uv publish --dry-run dist/*
  exit 0
fi

UV_PUBLISH_TOKEN="$token" uv publish dist/*

echo
echo "published. checking the index:"
python3 - "$version" <<'PY'
import json
import sys
import time
import urllib.request

wanted = sys.argv[1].replace("-alpha.", "a").replace("-beta.", "b").replace("-rc.", "rc")
for attempt in range(10):
    try:
        with urllib.request.urlopen("https://pypi.org/pypi/luna-doctor/json", timeout=20) as fh:
            data = json.load(fh)
    except Exception as exc:                      # index lag, or no network
        print(f"  attempt {attempt + 1}: {exc}")
        time.sleep(3)
        continue
    files = sorted(data["releases"])
    print(f"  versions on the index: {', '.join(files)}")
    if wanted in files:
        print(f"  {data['info']['name']} {data['info']['version']} is live")
        print(f"  https://pypi.org/project/luna-doctor/{data['info']['version']}/")
        break
    time.sleep(3)
else:
    sys.exit(f"  {wanted} did not appear on the index; check the upload log")
PY
