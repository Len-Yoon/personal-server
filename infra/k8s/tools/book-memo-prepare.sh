#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="personal-server"
APP="book-memo"
CLAIM="book-memo-data"
SENTINEL_IMAGE="personal-server-book-memo:unconfigured-do-not-run"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
MANIFEST="$(CDPATH= cd -- "$SCRIPT_DIR/../apps" && pwd)/book-memo.yaml"

fail() {
  printf 'book_memo_prepare=FAIL stage=%s\n' "$1" >&2
  exit 1
}

usage() {
  printf 'Usage: %s --go --image personal-server-book-memo@sha256:<64-lowercase-hex>\n' "$0" >&2
  exit 2
}

go=false
image=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --go) [ "$go" = false ] || fail arguments; go=true; shift ;;
    --image) [ -z "$image" ] && [ "$#" -ge 2 ] || fail arguments; image="$2"; shift 2 ;;
    --help) usage ;;
    *) fail arguments ;;
  esac
done

[ "$go" = true ] || fail arguments
[[ "$image" =~ ^docker\.io/library/personal-server-book-memo@sha256:[0-9a-f]{64}$ ]] || fail image
[ -f "$MANIFEST" ] || fail manifest

# Git checkouts on Windows may retain CRLF. Normalize only line endings while
# preserving the exact one-line replica-zero and sentinel-image contracts.
manifest_line_count() {
  sed 's/\r$//' "$MANIFEST" | grep -Fxc "$1"
}

[ "$(manifest_line_count '  replicas: 0')" -eq 1 ] || fail manifest
[ "$(manifest_line_count "          image: $SENTINEL_IMAGE")" -eq 1 ] || fail manifest

# Only existence is checked; Secret values are never read or emitted.
sudo -n k3s kubectl -n "$NAMESPACE" get secret book-memo-runtime >/dev/null 2>&1 || fail secret

image_digest="${image#*@}"
if ! sudo -n k3s ctr images list 2>/dev/null | awk \
  -v expected_image="$image" \
  -v expected_digest="$image_digest" '
    NR == 1 { next }
    $1 == expected_image && $3 == expected_digest {
      for (field_index = 4; field_index <= NF; field_index++) {
        if ($field_index ~ /(^|,)linux\/amd64(,|$)/) found = 1
      }
    }
    END { exit(found ? 0 : 1) }
  '; then
  fail image
fi

resource_absent() {
  local existing
  if ! existing="$(sudo -n k3s kubectl -n "$NAMESPACE" get "$1" "$2" --ignore-not-found -o name 2>/dev/null)"; then
    fail resource_check
  fi
  [ -z "$existing" ] || fail existing_resource
}

resource_absent pvc "$CLAIM"
resource_absent deployment "$APP"
resource_absent service "$APP"

render_manifest() {
  printf '%s\n' "$rendered_manifest"
}

validate_rendered_manifest() {
  python3 -c '
import json
import sys

expected_image = sys.argv[1]
payload = sys.stdin.read()
decoder = json.JSONDecoder()
documents = []
position = 0
while position < len(payload):
    while position < len(payload) and payload[position].isspace():
        position += 1
    if position == len(payload):
        break
    document, position = decoder.raw_decode(payload, position)
    if not isinstance(document, dict):
        raise SystemExit(1)
    documents.append(document)
if len(documents) == 1 and documents[0].get("kind") == "List":
    items = documents[0].get("items")
else:
    items = documents
if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
    raise SystemExit(1)
if len(items) != 3:
    raise SystemExit(1)
expected = {
    ("PersistentVolumeClaim", "book-memo-data"),
    ("Deployment", "book-memo"),
    ("Service", "book-memo"),
}
actual = {(item.get("kind"), item.get("metadata", {}).get("name")) for item in items}
if actual != expected or any(item.get("metadata", {}).get("namespace") != "personal-server" for item in items):
    raise SystemExit(1)
pvc = next(item for item in items if item.get("kind") == "PersistentVolumeClaim")
if pvc.get("spec", {}).get("accessModes") != ["ReadWriteOnce"]:
    raise SystemExit(1)
deployment = next(item for item in items if item.get("kind") == "Deployment")
containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
if deployment.get("spec", {}).get("replicas") != 0 or len(containers) != 1:
    raise SystemExit(1)
pod = deployment["spec"]["template"]["spec"]
if pod.get("initContainers") or pod.get("ephemeralContainers"):
    raise SystemExit(1)
if containers[0].get("image") != expected_image:
    raise SystemExit(1)
if containers[0].get("volumeMounts") != [
    {"name": "book-memo-data", "mountPath": "/data/book-memo"},
    {"name": "tmp", "mountPath": "/tmp"},
]:
    raise SystemExit(1)
if pod.get("volumes") != [
    {"name": "book-memo-data", "persistentVolumeClaim": {"claimName": "book-memo-data"}},
    {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}},
]:
    raise SystemExit(1)
service = next(item for item in items if item.get("kind") == "Service")
if service.get("spec", {}).get("selector") != {"app.kubernetes.io/name": "book-memo"}:
    raise SystemExit(1)
' "$image"
}

validate_applied_deployment() {
  python3 -c '
import json
import sys

expected_image = sys.argv[1]
deployment = json.load(sys.stdin)
metadata = deployment.get("metadata", {})
containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
if metadata.get("name") != "book-memo" or metadata.get("namespace") != "personal-server":
    raise SystemExit(1)
if deployment.get("spec", {}).get("replicas") != 0 or len(containers) != 1:
    raise SystemExit(1)
if containers[0].get("image") != expected_image:
    raise SystemExit(1)
' "$image"
}

rendered_manifest="$(sed "s|$SENTINEL_IMAGE|$image|" "$MANIFEST")" || fail manifest
[ "$(printf '%s\n' "$rendered_manifest" | grep -Foc "$SENTINEL_IMAGE")" -eq 0 ] || fail manifest
if ! render_manifest | sudo -n k3s kubectl -n "$NAMESPACE" create --dry-run=server -o json -f - 2>/dev/null | validate_rendered_manifest; then
  fail server_dry_run
fi

render_manifest | sudo -n k3s kubectl -n "$NAMESPACE" create -f - >/dev/null 2>&1 || fail create
if ! sudo -n k3s kubectl -n "$NAMESPACE" get deployment "$APP" -o json 2>/dev/null | validate_applied_deployment; then
  fail applied_contract
fi

printf '%s\n' 'book_memo_prepare=PASS'
