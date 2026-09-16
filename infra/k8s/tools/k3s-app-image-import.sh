#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'image_import=FAIL: %s\n' "$*" >&2
  exit 1
}

usage() {
  printf 'Usage: %s --go --archive <oci-archive> --sha256 <digest> --image <image-ref>\n' "$0" >&2
  exit 2
}

verify_oci_linux_amd64() {
  python3 - "$1" <<'PY'
import json
import sys
import tarfile

archive = sys.argv[1]
with tarfile.open(archive, "r:*") as source:
    index = json.load(source.extractfile("index.json"))
    manifests = index.get("manifests", [])
    if not manifests:
        raise ValueError("OCI index has no manifests")
    for manifest in manifests:
        platform = manifest.get("platform")
        if platform == {"os": "linux", "architecture": "amd64"}:
            sys.exit(0)
        digest = manifest.get("digest", "")
        if not digest.startswith("sha256:"):
            continue
        manifest_path = "blobs/sha256/" + digest.split(":", 1)[1]
        image_manifest = json.load(source.extractfile(manifest_path))
        config_digest = image_manifest.get("config", {}).get("digest", "")
        if not config_digest.startswith("sha256:"):
            continue
        config_path = "blobs/sha256/" + config_digest.split(":", 1)[1]
        config = json.load(source.extractfile(config_path))
        if config.get("os") == "linux" and config.get("architecture") == "amd64":
            sys.exit(0)
sys.exit(1)
PY
}

go=false
archive=""
digest=""
image=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --go) go=true; shift ;;
    --archive) archive="${2:-}"; shift 2 ;;
    --sha256) digest="${2:-}"; shift 2 ;;
    --image) image="${2:-}"; shift 2 ;;
    --help) usage ;;
    *) fail "unsupported option: $1" ;;
  esac
done

[ "$go" = true ] || fail "--go is required"
[ -s "$archive" ] || fail "archive is missing"
[[ "$digest" =~ ^[0-9a-fA-F]{64}$ ]] || fail "sha256 digest is invalid"
[[ "$image" =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]*$ ]] || fail "image reference is invalid"
case "$image" in *:latest) fail "latest tag is not allowed" ;; esac

printf '%s  %s\n' "$digest" "$archive" | sha256sum --check --status || fail "archive digest mismatch"
verify_oci_linux_amd64 "$archive" || fail "archive platform is not linux/amd64"
sudo -n k3s kubectl get node -o name >/dev/null || fail "K3s node is unavailable"
sudo -n k3s ctr images import "$archive" || fail "containerd import failed"
sudo -n k3s ctr images list -q | grep -Fxq "$image" || fail "imported image is unavailable"
printf '%s\n' 'image_import=PASS'
