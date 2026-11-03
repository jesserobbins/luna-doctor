#!/usr/bin/env bash
# Release luna-doctor: bump the version everywhere, roll the changelog, commit, tag.
#
#   scripts/release.sh patch      # 0.1.0 -> 0.1.1; from 0.1.1-alpha.3 -> 0.1.2 (suffix dropped)
#   scripts/release.sh minor      # 0.1.0 -> 0.2.0; from 0.1.1-alpha.3 -> 0.2.0
#   scripts/release.sh major      # 0.1.0 -> 1.0.0
#   scripts/release.sh alpha      # 0.1.1-alpha.3 -> 0.1.1-alpha.4; from 0.1.1 or
#                                 #   0.1.1-beta.2 -> 0.1.1-alpha.1
#   scripts/release.sh beta       # likewise on beta
#   scripts/release.sh rc         # likewise on rc
#   scripts/release.sh 0.4.2      # explicit final
#   scripts/release.sh 0.5.0-rc.2 # explicit prerelease
#   scripts/release.sh patch --allow-dirty
#
# A version is X.Y.Z or X.Y.Z-<alpha|beta|rc>.N. An alpha/beta/rc bump counts
# the prerelease up within the current X.Y.Z, starting at .1 when the current
# version is final or carries a different prerelease; a major/minor/patch bump
# drops the suffix, as it does from a final.
# It never pushes. When it is done it prints the push commands.

set -euo pipefail

cd "$(dirname "$0")/.."

bump="${1:-}"
allow_dirty=0
[ "${2:-}" = "--allow-dirty" ] && allow_dirty=1

if [ -z "$bump" ]; then
  echo "usage: scripts/release.sh <major|minor|patch|alpha|beta|rc|x.y.z[-alpha.N|-beta.N|-rc.N]> [--allow-dirty]" >&2
  exit 2
fi

if [ "$allow_dirty" = 0 ] && [ -n "$(git status --porcelain)" ]; then
  echo "working tree is dirty; commit first, or pass --allow-dirty" >&2
  git status --porcelain >&2
  exit 1
fi

# X.Y.Z, or X.Y.Z with one prerelease: -alpha.N, -beta.N, -rc.N
semver='^[0-9]+\.[0-9]+\.[0-9]+(-(alpha|beta|rc)\.[0-9]+)?$'

old="$(tr -d '[:space:]' < VERSION)"
if ! printf '%s' "$old" | grep -Eq "$semver"; then
  echo "VERSION does not hold a semver number: $old" >&2
  exit 1
fi

core="${old%%-*}"    # X.Y.Z, the part before any prerelease
IFS=. read -r major minor patch <<<"$core"
pre=""               # alpha | beta | rc, empty when the current version is final
n=0                  # the current prerelease counter
case "$old" in
  *-*) pre="${old#*-}"; pre="${pre%.*}"; n="${old##*.}" ;;
esac

case "$bump" in
  major) new="$((major + 1)).0.0" ;;
  minor) new="${major}.$((minor + 1)).0" ;;
  patch) new="${major}.${minor}.$((patch + 1))" ;;
  alpha|beta|rc)
    # same prerelease: count up within the current X.Y.Z;
    # final or a different prerelease: start at .1
    if [ "$pre" = "$bump" ]; then
      new="$core-$bump.$((n + 1))"
    else
      new="$core-$bump.1"
    fi
    ;;
  *)
    if ! printf '%s' "$bump" | grep -Eq "$semver"; then
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

# the constant in the script (single line, so a targeted replace is safe;
# the class matches the whole old number, prerelease hyphen included)
sed "s/^VERSION = \"[0-9][0-9A-Za-z.-]*\"\( *\)#/VERSION = \"$new\"\1#/" luna_doctor/cli.py > luna_doctor/cli.py.new
mv luna_doctor/cli.py.new luna_doctor/cli.py

if ! grep -q "^VERSION = \"$new\"" luna_doctor/cli.py; then
  echo "failed to update VERSION in luna_doctor/cli.py" >&2
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
