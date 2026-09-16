#!/usr/bin/env bash
set -euo pipefail

SUPPORTED_APPS="book-memo youtube-memo crawler-worker car-care-worker"

fail() {
  printf 'image_build=FAIL: %s\n' "$*" >&2
  exit 1
}

usage() {
  printf 'Usage: %s --app <app> --tag <immutable-tag> --output <oci-archive> [--dockerfile <path>]\n' "$0" >&2
  exit 2
}

app=""
tag=""
output=""
dockerfile=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --app) app="${2:-}"; shift 2 ;;
    --tag) tag="${2:-}"; shift 2 ;;
    --output) output="${2:-}"; shift 2 ;;
    --dockerfile) dockerfile="${2:-}"; shift 2 ;;
    --help) usage ;;
    *) fail "unsupported option: $1" ;;
  esac
done

test "$(uname -s)" = Darwin || fail "build must run on macOS"
case "$app" in
  book-memo|youtube-memo|crawler-worker|car-care-worker) ;;
  *) fail "unsupported app" ;;
esac
test -n "$tag" && test "$tag" != latest || fail "immutable tag is required"
[[ "$tag" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || fail "invalid immutable tag"
test -n "$output" || fail "output archive is required"
test -n "$dockerfile" || dockerfile="$app/Dockerfile"
test -f "$dockerfile" || fail "Dockerfile is missing"

image="personal-server-$app:$tag"
docker buildx build --platform linux/amd64 --tag "$image" --file "$dockerfile" --output type=oci,dest="$output" "$app"

if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$output"
else
  shasum -a 256 "$output"
fi
printf '%s\n' 'image_build=PASS'
