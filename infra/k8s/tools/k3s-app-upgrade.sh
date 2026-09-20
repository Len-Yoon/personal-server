#!/usr/bin/env bash
set -o pipefail

NAMESPACE="personal-server"
PORTAL_HEALTH_URL="https://len.pe.kr/health"
KUBECTL_REQUEST_TIMEOUT="15s"
CURL_CONNECT_TIMEOUT_SECONDS="5"
CURL_MAX_TIME_SECONDS="15"

fail() {
  printf 'k3s_app_upgrade=FAIL: %s\n' "$*" >&2
  exit 1
}

usage() {
  printf 'Usage: %s --check --app <portal-web|crawler-worker> --image <canonical-digest-ref>\n' "$0" >&2
  printf '       %s --go --app <portal-web|crawler-worker> --image <canonical-digest-ref> --expected-current-image <current-ref>\n' "$0" >&2
  exit 2
}

mode=""
app=""
image=""
expected_current_image=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check|--go) [ -z "$mode" ] || fail "only one mode is allowed"; mode="$1"; shift ;;
    --app) app="$2"; shift 2 ;;
    --image) image="$2"; shift 2 ;;
    --expected-current-image) expected_current_image="$2"; shift 2 ;;
    --help) usage ;;
    *) fail "unsupported option: $1" ;;
  esac
done

[ -n "$mode" ] || fail "--check or --go is required"
case "$app" in
  portal-web) container_name="portal-web"; service_port="8000" ;;
  crawler-worker) container_name="crawler-worker"; service_port="8001" ;;
  *) fail "unsupported app" ;;
esac

[ -n "$image" ] || fail "--image is required"
expected_image_prefix="docker.io/library/personal-server-$app@sha256:"
case "$image" in
  "$expected_image_prefix"*) ;;
  *) fail "image must be the canonical immutable digest for $app" ;;
esac
[[ "$image" =~ ^docker\.io/library/personal-server-(portal-web|crawler-worker)@sha256:[0-9a-f]{64}$ ]] ||
  fail "image must be an immutable sha256 digest"
[ "$mode" != "--go" ] || [ -n "$expected_current_image" ] ||
  fail "--expected-current-image is required with --go"

kube() {
  sudo -n k3s kubectl -n "$NAMESPACE" --request-timeout="$KUBECTL_REQUEST_TIMEOUT" "$@"
}

read_deployment_image() {
  local deployment_json
  deployment_json="$(kube get deployment "$app" -o json)" || return 1
  python3 - "$container_name" "$service_port" "$deployment_json" <<'PY'
import json
import sys
container_name, expected_port, deployment_payload = sys.argv[1:]
deployment = json.loads(deployment_payload)
spec = deployment.get("spec", {})
status = deployment.get("status", {})
containers = spec.get("template", {}).get("spec", {}).get("containers", [])
if spec.get("replicas") != 1 or status.get("readyReplicas") != 1 or status.get("availableReplicas") != 1:
    sys.exit(1)
if len(containers) != 1:
    sys.exit(1)
container = containers[0]
if container.get("name") != container_name:
    sys.exit(1)
if not any(port.get("name") == "http" and str(port.get("containerPort")) == expected_port for port in container.get("ports", [])):
    sys.exit(1)
image = container.get("image")
if not isinstance(image, str) or not image:
    sys.exit(1)
print(image)
PY
}

verify_containerd_image() {
  local expected_digest
  expected_digest="${image##*@}"
  sudo -n k3s ctr images list | awk -v image="$image" -v digest="$expected_digest" '
    $1 == image {
      count++
      if ($3 == digest) matching_digest++
      for (field = 1; field <= NF; field++) if ($field == "linux/amd64") amd64 = 1
    }
    END { exit !(count == 1 && matching_digest == 1 && amd64) }
  '
}

verify_service_endpoint() {
  local required_pod_name="$1"
  local service_json endpoints_json
  service_json="$(kube get service "$app" -o json)" || return 1
  endpoints_json="$(kube get endpoints "$app" -o json)" || return 1
  python3 - "$service_port" "$required_pod_name" <<'PY' "$service_json" "$endpoints_json"
import json
import sys
expected_port, required_pod_name, service_payload, endpoints_payload = sys.argv[1:]
service = json.loads(service_payload)
endpoints = json.loads(endpoints_payload)
ports = service.get("spec", {}).get("ports", [])
if (service.get("spec", {}).get("type") not in {"ClusterIP", "NodePort"} or not service.get("spec", {}).get("clusterIP") or not any(port.get("name") == "http" and str(port.get("port")) == expected_port and port.get("targetPort") == "http" for port in ports)):
    sys.exit(1)
for subset in endpoints.get("subsets", []):
    for address in subset.get("addresses", []):
        for port in subset.get("ports", []):
            target_ref = address.get("targetRef", {})
            if (
                address.get("ip")
                and str(port.get("port")) == expected_port
                and (
                    not required_pod_name
                    or (
                        target_ref.get("kind") == "Pod"
                        and target_ref.get("name") == required_pod_name
                    )
                )
            ):
                print("http://%s:%s/health" % (address["ip"], expected_port))
                sys.exit(0)
sys.exit(1)
PY
}

ready_pod_name() {
  local expected_image="$1"
  local pods_json
  pods_json="$(kube get pods -l "app.kubernetes.io/name=$app" -o json)" || return 1
  python3 - "$container_name" "$expected_image" "$pods_json" <<'PY'
import json
import sys
container_name, expected_image, pods_payload = sys.argv[1:]
for pod in json.loads(pods_payload).get("items", []):
    if pod.get("status", {}).get("phase") != "Running":
        continue
    statuses = pod.get("status", {}).get("containerStatuses", [])
    if any(
        item.get("name") == container_name
        and item.get("ready") is True
        and (item.get("image") == expected_image or item.get("imageID") == expected_image)
        for item in statuses
    ):
        print(pod.get("metadata", {}).get("name", ""))
        sys.exit(0)
sys.exit(1)
PY
}

preflight() {
  current_image="$(read_deployment_image)" || return 1
  verify_service_endpoint "" >/dev/null || return 1
}

patch_image() {
  local from_image="$1"
  local to_image="$2"
  local patch
  patch="$(python3 - "$from_image" "$to_image" <<'PY'
import json
import sys
path = "/spec/template/spec/containers/0/image"
print(json.dumps([
    {"op": "test", "path": path, "value": sys.argv[1]},
    {"op": "replace", "path": path, "value": sys.argv[2]},
], separators=(",", ":")))
PY
)" || return 1
  kube patch deployment "$app" --type=json --patch "$patch"
}

runtime_health() {
  local expected_image="$1"
  local pod_name endpoint_url status probe
  pod_name="$(ready_pod_name "$expected_image")" || return 1
  [ -n "$pod_name" ] || return 1
  endpoint_url="$(verify_service_endpoint "$pod_name")" || return 1
  curl --fail --silent --show-error --connect-timeout "$CURL_CONNECT_TIMEOUT_SECONDS" \
    --max-time "$CURL_MAX_TIME_SECONDS" "$endpoint_url" >/dev/null || return 1

  if [ "$app" = "portal-web" ]; then
    for probe in 1 2 3; do
      status="$(curl --silent --show-error --connect-timeout "$CURL_CONNECT_TIMEOUT_SECONDS" \
        --max-time "$CURL_MAX_TIME_SECONDS" --output /dev/null --write-out '%{http_code}' \
        "$PORTAL_HEALTH_URL")" || return 1
      [ "$status" = "200" ] || return 1
      [ "$probe" = 3 ] || sleep 10
    done
  fi
}

rollout_and_health() {
  local expected_image="$1"
  kube rollout status "deployment/$app" --timeout=120s >/dev/null || return 1
  runtime_health "$expected_image"
}

verify_containerd_image || fail "containerd target manifest digest or linux/amd64 preflight failed"
preflight || fail "deployment readiness or Service/Endpoint preflight failed"

if [ "$mode" = "--check" ]; then
  runtime_health "$current_image" || fail "active application health verification failed"
  printf 'k3s_app_upgrade=PASS mode=check app=%s image=%s\n' "$app" "$image"
  exit 0
fi

[ "$current_image" = "$expected_current_image" ] || fail "expected current image mismatch"
current_image="$(read_deployment_image)" || fail "deployment readiness preflight changed before patch"
[ "$current_image" = "$expected_current_image" ] || fail "expected current image mismatch"

patch_image "$current_image" "$image" || fail "target image patch failed; rollback was not attempted"
if rollout_and_health "$image"; then
  printf 'k3s_app_upgrade=PASS mode=go app=%s image=%s\n' "$app" "$image"
  exit 0
fi

if ! patch_image "$image" "$current_image"; then
  printf 'k3s_app_upgrade=FAIL: target rollout or health failed; rollback=FAIL\n' >&2
  exit 1
fi
if ! rollout_and_health "$current_image"; then
  printf 'k3s_app_upgrade=FAIL: target rollout or health failed; rollback=FAIL\n' >&2
  exit 1
fi

printf 'k3s_app_upgrade=FAIL: target rollout or health failed; rollback=PASS\n' >&2
exit 1
