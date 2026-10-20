#!/usr/bin/env bash
# Release luna-doctor: bump the version everywhere, roll the changelog, commit, tag.
#
#   scripts/release.sh patch      # 0.1.0 -> 0.1.1
#   scripts/release.sh minor      # 0.1.0 -> 0.2.0
#   scripts/release.sh major      # 0.1.0 -> 1.0.0
#   scripts/release.sh 0.4.2      # explicit
#   scripts/release.sh patch --allow-dirty
#
# It never pushes. When it is done it prints the push commands.

set -euo pipefail

cd "$(dirname "$0")/.."

bump="${1:-}"
allow_dirty=0
[ "${2:-}" = "--allow-dirty" ] && allow_dirty=1

if [ -z "$bump" ]; then
  echo "usage: scripts/release.sh <major|minor|patch|x.y.z> [--allow-dirty]" >&2
  exit 2
fi

if [ "$allow_dirty" = 0 ] && [ -n "$(git status --porcelain)" ]; then
  echo "working tree is dirty; commit first, or pass --allow-dirty" >&2
  git status --porcelain >&2
  exit 1
fi

old="$(tr -d '[:space:]' < VERSION)"
if ! printf '%s' "$old" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "VERSION does not hold a semver number: $old" >&2
  exit 1
fi
IFS=. read -r major minor patch <<<"$old"

case "$bump" in
  major) new="$((major + 1)).0.0" ;;
  minor) new="${major}.$((minor + 1)).0" ;;
  patch) new="${major}.${minor}.$((patch + 1))" ;;
  *)
    if ! printf '%s' "$bump" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
      echo "not a semver number: $bump" >&2
      exit 2
    fi
    new="$bump"
    ;;
esac

if [ "$new" = "$old" ]; then
  echo "version is already $old" >&2
  exit 1
fi

today="$(date -u +%Y-%m-%d)"

# VERSION file
printf '%s\n' "$new" > VERSION

# the constant in the script (single line, so a targeted replace is safe)
sed "s/^VERSION = \"[0-9][0-9.]*\"\( *\)#/VERSION = \"$new\"\1#/" bin/luna-doctor > bin/luna-doctor.new
mv bin/luna-doctor.new bin/luna-doctor
chmod +x bin/luna-doctor

if ! grep -q "^VERSION = \"$new\"" bin/luna-doctor; then
  echo "failed to update VERSION in bin/luna-doctor" >&2
  exit 1
fi

# changelog: close Unreleased, open a dated section under it
python3 - "$new" "$today" <<'PY'
import sys
new, today = sys.argv[1], sys.argv[2]
path = "CHANGELOG.md"
text = open(path).read()
if f"## [{new}]" in text:
    sys.exit(f"CHANGELOG.md already has a section for {new}")
head = "## [Unreleased]\n"
if head not in text:
    sys.exit("CHANGELOG.md is missing an '## [Unreleased]' heading")
text = text.replace(head, head + f"\n## [{new}] - {today}\n", 1)
open(path, "w").write(text)
print(f"changelog: opened ## [{new}] - {today}")
PY

echo
echo "summary"
echo "  old version: $old"
echo "  new version: $new"
echo
echo "next steps (nothing has been pushed):"
echo "  1. edit CHANGELOG.md: move what changed under ## [$new] - $today"
echo "  2. git add -A && git commit -m 'Release v$new'"
echo "  3. git tag -a v$new -m 'v$new'"
echo "  4. git push && git push --tags"
