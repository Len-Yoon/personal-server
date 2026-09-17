#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="personal-server"
APP="youtube-memo"
CLAIM="youtube-memo-data"
SENTINEL_IMAGE="personal-server-youtube-memo:unconfigured-do-not-run"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
MANIFEST="$(CDPATH= cd -- "$SCRIPT_DIR/../apps" && pwd)/youtube-memo.yaml"

fail() {
  printf 'youtube_memo_prepare=FAIL stage=%s\n' "$1" >&2
  exit 1
}

usage() {
  printf 'Usage: %s (--go|--bind-existing) --image personal-server-youtube-memo@sha256:<64-lowercase-hex>\n' "$0" >&2
  exit 2
}

go=false
bind_existing=false
image=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --go) [ "$go" = false ] || fail arguments; go=true; shift ;;
    --bind-existing) [ "$bind_existing" = false ] || fail arguments; bind_existing=true; shift ;;
    --image) [ -z "$image" ] && [ "$#" -ge 2 ] || fail arguments; image="$2"; shift 2 ;;
    --help) usage ;;
    *) fail arguments ;;
  esac
done

[ "$go" != "$bind_existing" ] || fail arguments
[[ "$image" =~ ^docker\.io/library/personal-server-youtube-memo@sha256:[0-9a-f]{64}$ ]] || fail image
[ -f "$MANIFEST" ] || fail manifest

# Git checkouts on Windows may retain CRLF. Normalize only line endings while
# preserving the exact one-line replica-zero and sentinel-image contracts.
manifest_line_count() {
  sed 's/\r$//' "$MANIFEST" | grep -Fxc "$1"
}

[ "$(manifest_line_count '  replicas: 0')" -eq 1 ] || fail manifest
[ "$(manifest_line_count "          image: $SENTINEL_IMAGE")" -eq 1 ] || fail manifest

# Only existence is checked; Secret values are never read or emitted.
sudo -n k3s kubectl -n "$NAMESPACE" get secret youtube-memo-runtime >/dev/null 2>&1 || fail secret

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
    ("PersistentVolumeClaim", "youtube-memo-data"),
    ("Deployment", "youtube-memo"),
    ("Service", "youtube-memo"),
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
    {"name": "youtube-memo-data", "mountPath": "/data/youtube-memo"},
    {"name": "tmp", "mountPath": "/tmp"},
]:
    raise SystemExit(1)
if pod.get("volumes") != [
    {"name": "youtube-memo-data", "persistentVolumeClaim": {"claimName": "youtube-memo-data"}},
    {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}},
]:
    raise SystemExit(1)
service = next(item for item in items if item.get("kind") == "Service")
if service.get("spec", {}).get("selector") != {"app.kubernetes.io/name": "youtube-memo"}:
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
if metadata.get("name") != "youtube-memo" or metadata.get("namespace") != "personal-server":
    raise SystemExit(1)
if deployment.get("spec", {}).get("replicas") != 0 or len(containers) != 1:
    raise SystemExit(1)
if containers[0].get("image") != expected_image:
    raise SystemExit(1)
' "$image"
}

validate_existing_resources() {
  {
    sudo -n k3s kubectl -n "$NAMESPACE" get pvc "$CLAIM" -o json 2>/dev/null
    sudo -n k3s kubectl -n "$NAMESPACE" get deployment "$APP" -o json 2>/dev/null
    sudo -n k3s kubectl -n "$NAMESPACE" get service "$APP" -o json 2>/dev/null
    sudo -n k3s kubectl -n "$NAMESPACE" get pods -o json 2>/dev/null
  } | python3 -c '
import json
import sys

image = sys.argv[1]
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
if len(documents) != 4:
    raise SystemExit(1)
pvc, deployment, service, pods = documents
if (pvc.get("metadata", {}).get("name") != "youtube-memo-data"
        or pvc.get("metadata", {}).get("namespace") != "personal-server"
        or pvc.get("spec", {}).get("accessModes") != ["ReadWriteOnce"]
        or pvc.get("spec", {}).get("resources", {}).get("requests", {}).get("storage") != "1Gi"
        or pvc.get("status", {}).get("phase") not in {"Pending", "Bound"}):
    raise SystemExit(1)
metadata = deployment.get("metadata", {})
spec = deployment.get("spec", {})
pod = spec.get("template", {}).get("spec", {})
containers = pod.get("containers", [])
if (metadata.get("name") != "youtube-memo" or metadata.get("namespace") != "personal-server"
        or spec.get("replicas") != 0 or spec.get("strategy") != {"type": "Recreate"}
        or spec.get("selector") != {"matchLabels": {"app.kubernetes.io/name": "youtube-memo"}}
        or spec.get("template", {}).get("metadata", {}).get("labels") != {"app.kubernetes.io/name": "youtube-memo"}
        or "initContainers" in pod or "ephemeralContainers" in pod
        or pod.get("automountServiceAccountToken") is not False
        or pod.get("securityContext") != {
            "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
            "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"},
        }
        or len(containers) != 1):
    raise SystemExit(1)
container = containers[0]
def exact_http_probe(value):
    return value in (
        {"path": "/health", "port": "http"},
        {"path": "/health", "port": "http", "scheme": "HTTP"},
    )
def exact_service_ports(value):
    return value in (
        [{"name": "http", "port": 8002, "targetPort": "http"}],
        [{"name": "http", "port": 8002, "targetPort": "http", "protocol": "TCP"}],
    )
if (container.get("name") != "youtube-memo" or container.get("image") != image
        or container.get("imagePullPolicy") != "Never"
        or container.get("envFrom") != [{"secretRef": {"name": "youtube-memo-runtime"}}]
        or "env" in container or "lifecycle" in container
        or container.get("securityContext") != {
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        }
        or not exact_http_probe(container.get("readinessProbe", {}).get("httpGet"))
        or not exact_http_probe(container.get("livenessProbe", {}).get("httpGet"))
        or container.get("volumeMounts") != [
            {"name": "youtube-memo-data", "mountPath": "/data/youtube-memo"},
            {"name": "tmp", "mountPath": "/tmp"},
        ]
        or pod.get("volumes") != [
            {"name": "youtube-memo-data", "persistentVolumeClaim": {"claimName": "youtube-memo-data"}},
            {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}},
        ]):
    raise SystemExit(1)
if (service.get("metadata", {}).get("name") != "youtube-memo"
        or service.get("metadata", {}).get("namespace") != "personal-server"
        or service.get("spec", {}).get("type") != "ClusterIP"
        or service.get("spec", {}).get("selector") != {"app.kubernetes.io/name": "youtube-memo"}
        or not exact_service_ports(service.get("spec", {}).get("ports"))):
    raise SystemExit(1)
items = pods.get("items")
if not isinstance(items, list) or any(
        any(volume.get("persistentVolumeClaim", {}).get("claimName") == "youtube-memo-data"
            for volume in item.get("spec", {}).get("volumes", []))
        for item in items):
    raise SystemExit(1)
' "$image"
}

binder_name=""
binder_owner=""
binder_uid=""

make_binder_manifest() {
  python3 - "$image" "$binder_name" "$binder_owner" <<'PY'
import json
import sys

image, name, owner = sys.argv[1:]
print(json.dumps({
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {
        "name": name,
        "namespace": "personal-server",
        "labels": {
            "app.kubernetes.io/managed-by": "youtube-memo-prepare",
            "personal-server.io/pvc-binder-owner": owner,
        },
    },
    "spec": {
        "automountServiceAccountToken": False,
        "restartPolicy": "Never",
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "runAsGroup": 10001,
            "fsGroup": 10001,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [{
            "name": "pvc-binder",
            "image": image,
            "imagePullPolicy": "Never",
            "command": ["python3", "-c", "import time; time.sleep(180)"],
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
            "volumeMounts": [{"name": "youtube-memo-data", "mountPath": "/data/youtube-memo", "readOnly": True}],
        }],
        "volumes": [{"name": "youtube-memo-data", "persistentVolumeClaim": {"claimName": "youtube-memo-data"}}],
    },
}, separators=(",", ":")))
PY
}

validate_binder_contract() {
  local require_uid="$1"
  local expected_uid="${2:-}"
  local binder_json
  binder_json="$(cat)"
  python3 -c '
import json
import sys

image, name, owner, require_uid, expected_uid = sys.argv[1:]
pod = json.load(sys.stdin)
metadata = pod.get("metadata", {})
labels = metadata.get("labels", {})
spec = pod.get("spec", {})
containers = spec.get("containers", [])
if (metadata.get("name") != name or metadata.get("namespace") != "personal-server"
        or labels.get("app.kubernetes.io/managed-by") != "youtube-memo-prepare"
        or labels.get("personal-server.io/pvc-binder-owner") != owner):
    raise SystemExit(1)
uid = metadata.get("uid")
if require_uid == "true" and (not isinstance(uid, str) or not uid or (expected_uid and uid != expected_uid)):
    raise SystemExit(1)
if spec.get("automountServiceAccountToken") is not False:
    raise SystemExit(1)
if spec.get("restartPolicy") != "Never":
    raise SystemExit(1)
if "initContainers" in spec or "ephemeralContainers" in spec:
    raise SystemExit(1)
if spec.get("securityContext") != {
    "runAsNonRoot": True,
    "runAsUser": 10001,
    "runAsGroup": 10001,
    "fsGroup": 10001,
    "seccompProfile": {"type": "RuntimeDefault"},
}:
    raise SystemExit(1)
if spec.get("volumes") != [{"name": "youtube-memo-data", "persistentVolumeClaim": {"claimName": "youtube-memo-data"}}]:
    raise SystemExit(1)
if len(containers) != 1:
    raise SystemExit(1)
container = containers[0]
if (container.get("name") != "pvc-binder" or container.get("image") != image
        or container.get("imagePullPolicy") != "Never"
        or container.get("command") != ["python3", "-c", "import time; time.sleep(180)"]
        or container.get("volumeMounts") != [{"name": "youtube-memo-data", "mountPath": "/data/youtube-memo", "readOnly": True}]
        or container.get("securityContext") != {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        }
        or any(key in container for key in (
            "args", "env", "envFrom", "lifecycle", "livenessProbe", "ports",
            "readinessProbe", "startupProbe", "stdin", "stdinOnce", "tty",
            "volumeDevices", "workingDir",
        ))):
    raise SystemExit(1)
if require_uid == "true":
    print(uid)
' "$image" "$binder_name" "$binder_owner" "$require_uid" "$expected_uid" <<<"$binder_json" 2>/dev/null
}

read_owned_binder_uid() {
  local expected_uid="${1:-}"
  local binder_json
  binder_json="$(sudo -n k3s kubectl -n "$NAMESPACE" get pod "$binder_name" -o json 2>/dev/null)" || return 1
  printf '%s' "$binder_json" | validate_binder_contract true "$expected_uid"
}

delete_owned_binder() {
  local observed_uid
  observed_uid="$(read_owned_binder_uid "$binder_uid")" || return 1
  [ "$observed_uid" = "$binder_uid" ] || return 1
  printf '{"apiVersion":"v1","kind":"DeleteOptions","preconditions":{"uid":"%s"}}' "$binder_uid" |
    sudo -n k3s kubectl -n "$NAMESPACE" delete \
      --raw="/api/v1/namespaces/$NAMESPACE/pods/$binder_name" -f - >/dev/null 2>&1 || return 1
  sudo -n k3s kubectl -n "$NAMESPACE" wait --for=delete "pod/$binder_name" --timeout=60s >/dev/null 2>&1 || return 1
  local remaining
  remaining="$(sudo -n k3s kubectl -n "$NAMESPACE" get pod "$binder_name" --ignore-not-found -o name 2>/dev/null)" || return 1
  [ -z "$remaining" ]
}

bind_pvc_without_writer() {
  binder_owner="$(python3 -c 'import secrets; print(secrets.token_hex(16))')" || fail binder
  [[ "$binder_owner" =~ ^[0-9a-f]{32}$ ]] || fail binder
  binder_name="youtube-memo-pvc-binder-$binder_owner"

  if ! make_binder_manifest | sudo -n k3s kubectl -n "$NAMESPACE" create --dry-run=server -o json -f - 2>/dev/null | validate_binder_contract false; then
    fail binder_contract
  fi
  if ! make_binder_manifest | sudo -n k3s kubectl -n "$NAMESPACE" create -o json -f - >/dev/null 2>&1; then
    binder_uid="$(read_owned_binder_uid)" || fail recovery
    delete_owned_binder || fail recovery
    fail recovery
  fi
  binder_uid="$(read_owned_binder_uid)" || fail recovery

  if ! sudo -n k3s kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/$binder_name" --timeout=60s >/dev/null 2>&1; then
    delete_owned_binder || true
    fail pvc_bind
  fi
  if ! sudo -n k3s kubectl -n "$NAMESPACE" wait --for='jsonpath={.status.phase}=Bound' "pvc/$CLAIM" --timeout=60s >/dev/null 2>&1; then
    delete_owned_binder || true
    fail pvc_bind
  fi
  delete_owned_binder || fail recovery
}

if [ "$go" = true ]; then
  resource_absent pvc "$CLAIM"
  resource_absent deployment "$APP"
  resource_absent service "$APP"

  rendered_manifest="$(sed "s|$SENTINEL_IMAGE|$image|" "$MANIFEST")" || fail manifest
  [ "$(printf '%s\n' "$rendered_manifest" | grep -Foc "$SENTINEL_IMAGE")" -eq 0 ] || fail manifest
  if ! render_manifest | sudo -n k3s kubectl -n "$NAMESPACE" create --dry-run=server -o json -f - 2>/dev/null | validate_rendered_manifest; then
    fail server_dry_run
  fi

  render_manifest | sudo -n k3s kubectl -n "$NAMESPACE" create -f - >/dev/null 2>&1 || fail create
  validate_existing_resources || fail existing
else
  validate_existing_resources || fail existing
fi

bind_pvc_without_writer

printf '%s\n' 'youtube_memo_prepare=PASS'
