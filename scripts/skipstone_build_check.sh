#!/bin/bash
# Build skipstone's macOS skip-macos.zip from our fork per VDS-BUILD.md, then check that it
# transpiles identically to the stock upstream binary of the same version.
#
# usage: skipstone_build_check.sh <upstream version> <fork ref> <vds tag> <out dir>
#   e.g. skipstone_build_check.sh 1.9.17 rebase/1.9.17 1.9.17-vds.1 out
# env:   SAMPLES   space-separated upstream sample packages to transpile
#                  (default: "skip-lib skip-foundation", each at its latest release)
#        VDS_ZIP   use this prebuilt zip instead of building (for local re-checks)
#
# Writes to <out dir>: skip-macos.zip, skip-macos.zip.sha256, check-summary.md, logs/.
# Exit status: 0 when every sample's normalised output is identical, 1 otherwise.
#
# The identical-output check: each sample package is built twice in the same directory
# (`swift build --build-tests`), once with a copy of skiptools/skip at <version> whose macOS
# binaryTarget points at the stock skip-macos.zip, once with one pointing at ours (attached with
# `swift package edit skip`). The generated trees under .build/plugins/outputs are compared file
# by file. The only difference allowed is the skipstone version string in the per-module
# .sourcehash markers ("skipstone":"<vds tag>" vs "skipstone":"<version>"), which is normalised.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ARTIFACT_CHECK="$SCRIPT_DIR/skipstone_artifact_check.py"
VERSION=${1:?version}; REF=${2:?ref}; VDS_TAG=${3:?vds tag}; OUT=${4:?out dir}
SAMPLES=${SAMPLES:-"skip-lib skip-foundation"}
mkdir -p "$OUT/logs"; OUT=$(cd "$OUT" && pwd)
SOURCE_IDENTITY="$OUT/source-commit.txt"
# A reused output directory must never attribute a prebuilt zip to an earlier source build.
rm -f "$SOURCE_IDENTITY"
WORK=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/skipstone-check.XXXXXX")
SUMMARY="$OUT/check-summary.md"
log() { echo "== $*" >&2; }

{
  echo "# skipstone $VDS_TAG build and identical-output check"
  echo
  echo "- fork ref: \`$REF\`; upstream version: \`$VERSION\`"
  echo "- host: $(sw_vers -productName) $(sw_vers -productVersion), $(uname -m)"
  echo "- Xcode: $(xcodebuild -version | tr '\n' ' ')"
  echo "- Swift: $(swift --version 2>&1 | head -1)"
} > "$SUMMARY"

# ---- 1. build ours
if [ -n "${VDS_ZIP:-}" ]; then
  cp "$VDS_ZIP" "$OUT/skip-macos.zip"
  echo "- binary: prebuilt \`$VDS_ZIP\` (not built here)" >> "$SUMMARY"
else
  log "clone vindhyadatascience/skipstone@$REF"
  git clone --quiet --branch "$REF" --recurse-submodules https://github.com/vindhyadatascience/skipstone.git "$WORK/skipstone"
  cd "$WORK/skipstone"
  SRC_SHA=$(git rev-parse HEAD)
  python3 "$ARTIFACT_CHECK" record-source "$SOURCE_IDENTITY" "$SRC_SHA"
  echo "- source commit: \`$SRC_SHA\`" >> "$SUMMARY"
  STAMP_FILE=Sources/SkipSyntax/Version.swift
  if ! grep -q "skipVersion = \"$VDS_TAG\"" "$STAMP_FILE"; then
    sed -i '' -E "s/(public let skipVersion = )\"[^\"]*\"/\\1\"$VDS_TAG\"/" "$STAMP_FILE"
    grep -q "skipVersion = \"$VDS_TAG\"" "$STAMP_FILE"
    echo "- \`skipVersion\` stamped as \`$VDS_TAG\` in the build tree only (not committed; commit it on vds/$VERSION before tagging)" >> "$SUMMARY"
  fi
  swift package resolve > "$OUT/logs/resolve.log" 2>&1
  if git ls-files --error-unmatch Package.resolved >/dev/null 2>&1; then
    git diff --exit-code Package.resolved > "$OUT/logs/resolved.diff" || { echo "- FAIL: swift package resolve changed the committed Package.resolved" >> "$SUMMARY"; exit 1; }
  else
    echo "- WARNING: Package.resolved is not committed on this ref; dependency versions are whatever resolved today" >> "$SUMMARY"
  fi
  log "build (scripts/build_macos_plugin.sh)"
  SKIP_VERSION="$VDS_TAG" bash scripts/build_macos_plugin.sh > "$OUT/logs/build.log" 2>&1 || { tail -50 "$OUT/logs/build.log"; echo "- FAIL: build failed" >> "$SUMMARY"; exit 1; }
  cp .build/artifactbundle-macos/skip-macos.zip "$OUT/skip-macos.zip"
fi
(cd "$OUT" && shasum -a 256 skip-macos.zip > skip-macos.zip.sha256)
echo "- \`skip-macos.zip\` SHA-256: \`$(cut -d' ' -f1 "$OUT/skip-macos.zip.sha256")\`" >> "$SUMMARY"

# ---- 2. stock binary, verified against the checksum upstream skip pins at $VERSION
log "fetch skiptools/skip@$VERSION and the stock binary"
git clone --quiet --depth 1 --branch "$VERSION" https://github.com/skiptools/skip.git "$WORK/skip-upstream"
STOCK_LINE=$(grep -E 'binaryTarget\(name: "skip", url: "[^"]*skip-macos\.zip"' "$WORK/skip-upstream/Package.swift" | head -1)
STOCK_URL=$(sed -E 's/.*url: "([^"]*)".*/\1/' <<<"$STOCK_LINE")
STOCK_SUM=$(sed -E 's/.*checksum: "([^"]*)".*/\1/' <<<"$STOCK_LINE")
curl -fsSL --retry 5 -o "$WORK/stock-skip-macos.zip" "$STOCK_URL"
echo "$STOCK_SUM  $WORK/stock-skip-macos.zip" | shasum -a 256 -c - >/dev/null
echo "- stock binary: $STOCK_URL (SHA-256 \`$STOCK_SUM\`, matches upstream skip's pin)" >> "$SUMMARY"

make_skip_copy() { # <dir> <zip>
  cp -R "$WORK/skip-upstream" "$1"
  cp "$2" "$1/skip-macos.zip"
  python3 - "$1/Package.swift" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p).read()
s2, n = re.subn(r'\.binaryTarget\(name: "skip", url: "[^"]*skip-macos\.zip", checksum: "[0-9a-f]+"\)',
                '.binaryTarget(name: "skip", path: "skip-macos.zip")', s)
assert n == 1, f"expected one macOS binaryTarget, found {n}"
open(p, "w").write(s2)
PY
}
make_skip_copy "$WORK/skip-stock" "$WORK/stock-skip-macos.zip"
make_skip_copy "$WORK/skip-vds" "$OUT/skip-macos.zip"

manifest() { # <outputs dir> <out file>
  local destination=$2 staging
  rm -f "$destination" || return 1
  staging=$(mktemp "${destination}.tmp.XXXXXX") || return 1
  if ! python3 "$ARTIFACT_CHECK" manifest "$1" "$staging" "$VERSION" "$VDS_TAG"; then
    rm -f "$staging"
    return 1
  fi
  if ! mv -f "$staging" "$destination"; then
    rm -f "$staging"
    return 1
  fi
}

run_variant() { # <sample dir> <variant>
  local dir=$1 variant=$2 status=0 manifest_path log_path
  manifest_path="$OUT/logs/$(basename "$dir")-$variant.manifest"
  log_path="$OUT/logs/$(basename "$dir")-$variant.log"
  # This function is called by if !, so errexit cannot propagate failures for us.
  rm -f "$manifest_path" || return 1
  if ! (cd "$dir" || exit 1
    rm -rf .build || exit 1
    swift package resolve || exit 1
    swift package edit skip --path "$WORK/skip-$variant" || exit 1
    swift build --build-tests || exit 1
    find .build/artifacts -maxdepth 5 | sed -n '1,20p' || exit 1
  ) > "$log_path" 2>&1; then
    tail -40 "$log_path" || true
    status=1
  elif ! manifest "$dir/.build/plugins/outputs" "$manifest_path"; then
    status=1
  fi
  (cd "$dir" && swift package unedit skip --force >/dev/null 2>&1) || true
  return "$status"
}

STATUS=0
echo >> "$SUMMARY"; echo "## Samples" >> "$SUMMARY"; echo >> "$SUMMARY"
for s in $SAMPLES; do
  tag=$(gh api "repos/skiptools/$s/releases/latest" --jq .tag_name 2>/dev/null || git ls-remote --tags --refs "https://github.com/skiptools/$s.git" | sed 's#.*refs/tags/##' | grep -E '^[0-9]+\.[0-9]+(\.[0-9]+)?$' | sort -V | tail -1)
  log "sample skiptools/$s@$tag"
  git clone --quiet --depth 1 --branch "$tag" "https://github.com/skiptools/$s.git" "$WORK/$s"
  if ! run_variant "$WORK/$s" stock || ! run_variant "$WORK/$s" vds; then
    echo "- FAIL \`$s@$tag\`: build failed (see logs)" >> "$SUMMARY"; STATUS=1; continue
  fi
  files=$(wc -l < "$OUT/logs/$s-stock.manifest" | tr -d ' ') || { echo "- FAIL: manifest count failed" >> "$SUMMARY"; STATUS=1; continue; }
  kt=$(python3 "$ARTIFACT_CHECK" count-kotlin "$OUT/logs/$s-stock.manifest") || { echo "- FAIL: Kotlin count failed" >> "$SUMMARY"; STATUS=1; continue; }
  if [ "$files" -eq 0 ] || [ "$kt" -eq 0 ]; then
    echo "- FAIL \`$s@$tag\`: no generated Kotlin files (the plugin did not transpile code)" >> "$SUMMARY"; STATUS=1
  elif diff "$OUT/logs/$s-stock.manifest" "$OUT/logs/$s-vds.manifest" > "$OUT/logs/$s.diff"; then
    echo "- PASS \`$s@$tag\`: $files generated files ($kt .kt) identical after normalising the skipstone version string" >> "$SUMMARY"
  else
    echo "- FAIL \`$s@$tag\`: $(grep -c '^[<>]' "$OUT/logs/$s.diff") manifest lines differ (logs/$s.diff)" >> "$SUMMARY"; STATUS=1
  fi
done
echo >> "$SUMMARY"
[ $STATUS -eq 0 ] && echo "**Result: PASS**" >> "$SUMMARY" || echo "**Result: FAIL**" >> "$SUMMARY"
cat "$SUMMARY"
exit $STATUS
