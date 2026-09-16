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
  python3 - "$1" "$2" <<'PY'
import json
import sys
import tarfile

archive = sys.argv[1]
image = sys.argv[2]
INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}


def read_blob_json(source, descriptor):
    digest = descriptor.get("digest", "")
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise ValueError("invalid OCI descriptor digest")
    digest_value = digest.split(":", 1)[1]
    if any(character not in "0123456789abcdef" for character in digest_value):
        raise ValueError("invalid OCI descriptor digest")
    member = source.extractfile("blobs/sha256/" + digest_value)
    if member is None:
        raise ValueError("OCI descriptor blob is missing")
    return json.load(member)


def resolves_to_linux_amd64(source, descriptor, visited):
    digest = descriptor.get("digest", "")
    if digest in visited:
        return False
    visited.add(digest)
    document = read_blob_json(source, descriptor)
    media_type = descriptor.get("mediaType", "")
    if media_type in INDEX_MEDIA_TYPES or "manifests" in document:
        manifests = document.get("manifests", [])
        return any(resolves_to_linux_amd64(source, child, visited) for child in manifests)

    config = document.get("config", {})
    if not isinstance(config, dict):
        return False
    config_document = read_blob_json(source, config)
    return (
        config_document.get("os") == "linux"
        and config_document.get("architecture") == "amd64"
    )


with tarfile.open(archive, "r:*") as source:
    index_member = source.extractfile("index.json")
    if index_member is None:
        raise ValueError("OCI index is missing")
    index = json.load(index_member)
    manifests = index.get("manifests", [])
    if not manifests:
        raise ValueError("OCI index has no manifests")
    selected = [
        manifest for manifest in manifests
        if manifest.get("annotations", {}).get("io.personal-server.image-ref") == image
    ]
    if len(selected) != 1:
        sys.exit(2)
    descriptor = selected[0]
    if not resolves_to_linux_amd64(source, descriptor, set()):
        sys.exit(3)
    print(descriptor["digest"])
PY
}

imported_image_digest() {
  awk -v image="$1" '
    function valid_digest(value, body) {
      if (value !~ /^sha256:/ || length(value) != 71) return 0
      body = substr(value, 8)
      return body !~ /[^0-9a-f]/
    }
    NR == 1 { next }
    $1 == image {
      if (!valid_digest($3)) exit 2
      count++
      digest = $3
    }
    END {
      if (count != 1) exit 1
      print digest
    }
  '
}

canonical_image_ref() {
  case "$1" in
    */*) printf '%s\n' "$1" ;;
    *) printf 'docker.io/library/%s\n' "$1" ;;
  esac
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
if archive_image_digest="$(verify_oci_linux_amd64 "$archive" "$image")"; then
  :
else
  case "$?" in
    3) fail "archive platform is not linux/amd64" ;;
    *) fail "archive image reference is missing or ambiguous" ;;
  esac
fi
sudo -n k3s kubectl get node -o name >/dev/null || fail "K3s node is unavailable"
sudo -n k3s ctr images import "$archive" || fail "containerd import failed"
containerd_image_ref="$(canonical_image_ref "$image")"
containerd_image_digest="$(sudo -n k3s ctr images list | imported_image_digest "$containerd_image_ref")" || fail "imported image digest is missing or ambiguous"
[ "$containerd_image_digest" = "$archive_image_digest" ] || fail "imported image digest does not match archive"
printf '%s\n' 'image_import=PASS'
