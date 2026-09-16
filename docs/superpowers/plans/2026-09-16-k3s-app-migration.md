# K3s 애플리케이션 전환 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 맥북에서 생성한 Linux AMD64 Book Memo 이미지를 N100 K3s에 안전하게 반입하고, 데이터·공개 경로를 보존한 서비스별 전환 기반을 구축함.

**Architecture:** 맥북은 테스트와 OCI 이미지 archive 생성을 담당하고, N100은 archive import와 K3s 적용만 담당함. 첫 구현은 Book Memo의 사전 점검·K3s 매니페스트·명시적 전환/롤백 도구로 한정하며, Compose와 K3s가 SQLite 파일을 동시에 쓰지 않도록 상태 전이를 fail-closed로 강제함.

**Tech Stack:** Bash, Docker Buildx, OCI archive, Tailscale SSH, K3s/containerd, Kubernetes YAML, Python unittest.

**Spec:** `docs/superpowers/specs/2026-09-16-k3s-app-migration-design.md`

## Global Constraints

- 맥북 ARM 환경에서 생성하는 앱 이미지는 반드시 `linux/amd64`로 지정함.
- 새 외부 컨테이너 레지스트리, image pull Secret, 자격 증명을 추가하지 않음.
- 실제 N100 이미지 반입·K3s 적용·데이터 복사·공개 경로 변경은 서비스별 사용자 승인 후에만 수행함.
- Compose와 K3s가 동일 SQLite 파일 또는 데이터 디렉터리에 동시에 쓰지 않음.
- Secret·토큰·OAuth 값·Telegram 값·개인 데이터는 Git·이미지·로그·문서에 기록하지 않음.
- Portal·Portal PVC·Caddy·Cloudflare Tunnel·Windows/WSL 부팅 복구·Docker 의존 운영 보조 서비스는 수정하지 않음.
- 한 서비스가 안정화되기 전 다음 서비스로 진행하지 않음.

---

## File Structure

| 파일 | 역할 |
|---|---|
| `infra/k8s/tools/k3s-app-image-build.sh` | 맥북에서 지정 앱의 Linux AMD64 OCI archive를 생성하고 digest를 출력함 |
| `infra/k8s/tools/k3s-app-image-import.sh` | N100에서 승인된 archive를 K3s containerd에 import하고 digest·플랫폼·이미지 존재를 검증함 |
| `infra/k8s/apps/book-memo.yaml` | Book Memo PVC·Deployment·ClusterIP Service의 선언형 정의를 보관함 |
| `infra/k8s/tools/book-memo-cutover.sh` | 사전 점검, 준비, 사용자 승인 전환, 롤백을 분리한 operator 전용 도구임 |
| `tests/test_k3s_app_image_transfer.py` | 이미지 플랫폼·archive·import 계약을 검증함 |
| `tests/test_k8s_book_memo_migration.py` | 매니페스트와 fail-closed 상태 전이 계약을 검증함 |
| `docs/operations-reference.md` | Book Memo K3s 전환의 승인·검증·롤백 절차를 문서화함 |

### Task 1: 이미지 생성·반입 계약

**Files:**
- Create: `infra/k8s/tools/k3s-app-image-build.sh`
- Create: `infra/k8s/tools/k3s-app-image-import.sh`
- Test: `tests/test_k3s_app_image_transfer.py`

**Interfaces:**
- Consumes: 앱 이름 하나(`book-memo`), Dockerfile 경로, 출력 OCI archive 경로.
- Produces: `personal-server-<app>:<immutable-tag>` Linux AMD64 OCI archive와 확인 가능한 sha256 digest.
- `k3s-app-image-build.sh --app book-memo --tag <immutable-tag> --output <archive>`는 macOS에서만 실행함.
- `k3s-app-image-import.sh --go --archive <archive> --sha256 <digest> --image <image-ref>`는 N100에서만 실행하며 `sudo -n k3s ctr images import`를 사용함.

- [ ] **Step 1: 이미지 도구 계약 테스트를 작성함.**

```python
def test_build_script_requires_supported_app_amd64_and_explicit_output(self):
    text = BUILD.read_text(encoding="utf-8")
    self.assertIn('SUPPORTED_APPS="book-memo youtube-memo crawler-worker car-care-worker"', text)
    self.assertIn("--platform linux/amd64", text)
    self.assertIn("--output type=oci,dest=", text)
    self.assertIn('uname -s', text)
    self.assertIn('Darwin', text)
    self.assertIn('latest', text)
    self.assertIn("image_build=PASS", text)

def test_import_script_requires_go_and_rejects_missing_archive_before_ctr_access(self):
    result = subprocess.run(["bash", str(IMPORT), "--archive", "/missing", "--image", "x:y"], capture_output=True, text=True)
    self.assertNotEqual(result.returncode, 0)
    self.assertIn("image_import=FAIL", result.stderr)
    self.assertNotIn("ctr images import", result.stdout + result.stderr)
```

- [ ] **Step 2: 테스트가 구현 부재로 실패하는지 확인함.**

Run: `python3 -m unittest tests.test_k3s_app_image_transfer -v`

Expected: FAIL because both scripts do not exist.

- [ ] **Step 3: 최소 이미지 생성 도구를 구현함.**

```bash
test "$(uname -s)" = Darwin || fail "build must run on macOS"
case "$app" in book-memo|youtube-memo|crawler-worker|car-care-worker) ;; *) fail "unsupported app" ;; esac
test -n "$tag" && test "$tag" != latest || fail "immutable tag is required"
docker buildx build --platform linux/amd64 --tag "$image" --output "type=oci,dest=$output" "$app"
sha256sum "$output"
printf '%s\n' 'image_build=PASS'
```

`latest` 태그를 허용하지 않고, archive 존재·비어 있지 않음·이미지 참조 문자 집합을 먼저 검증함.

- [ ] **Step 4: 최소 반입 도구를 구현함.**

```bash
[ "$go" = true ] || fail "--go is required"
[ -s "$archive" ] || fail "archive is missing"
printf '%s  %s\n' "$digest" "$archive" | sha256sum --check --status || fail "archive digest mismatch"
verify_oci_linux_amd64 "$archive" || fail "archive platform is not linux/amd64"
sudo -n k3s kubectl get node -o name >/dev/null || fail "K3s node is unavailable"
sudo -n k3s ctr images import "$archive" || fail "containerd import failed"
sudo -n k3s ctr images list -q | grep -Fxq "$image" || fail "imported image is unavailable"
printf '%s\n' 'image_import=PASS'
```

archive import 외의 Kubernetes 변경은 수행하지 않음.

- [ ] **Step 5: 테스트를 통과시킴.**

Run: `python3 -m unittest tests.test_k3s_app_image_transfer -v`

Expected: PASS.

- [ ] **Step 6: 커밋함.**

```bash
git add infra/k8s/tools/k3s-app-image-build.sh infra/k8s/tools/k3s-app-image-import.sh tests/test_k3s_app_image_transfer.py
git commit -m "feat: K3s 앱 이미지 반입 도구 추가"
```

### Task 2: Book Memo K3s 매니페스트와 저장소 경계

**Files:**
- Create: `infra/k8s/apps/book-memo.yaml`
- Test: `tests/test_k8s_book_memo_migration.py`

**Interfaces:**
- Consumes: `personal-server-book-memo:<immutable-tag>`, 사전 시딩된 runtime Secret 이름, 전용 `book-memo-data` PVC.
- Produces: `personal-server` namespace의 PVC, 1-replica Deployment, ClusterIP Service `book-memo`.
- 서비스는 container port `8003`만 노출하며 NodePort·LoadBalancer를 만들지 않음.

- [ ] **Step 1: 매니페스트 계약 테스트를 작성함.**

```python
def test_book_memo_manifest_has_single_nonroot_writer_and_clusterip_service(self):
    documents = list(yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")))
    deployment = next(item for item in documents if item["kind"] == "Deployment")
    service = next(item for item in documents if item["kind"] == "Service")
    self.assertEqual(deployment["spec"]["replicas"], 1)
    self.assertTrue(deployment["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"])
    self.assertEqual(service["spec"]["type"], "ClusterIP")
    self.assertNotIn("nodePort", yaml.safe_dump(service))
```

- [ ] **Step 2: 테스트가 매니페스트 부재로 실패하는지 확인함.**

Run: `python3 -m unittest tests.test_k8s_book_memo_migration.BookMemoManifestTests -v`

Expected: FAIL because the manifest does not exist.

- [ ] **Step 3: 최소 매니페스트를 구현함.**

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: book-memo-data
  namespace: personal-server
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: book-memo
  namespace: personal-server
spec:
  replicas: 1
```

Deployment은 `imagePullPolicy: Never`, `runAsNonRoot: true`, read-only root filesystem, `/tmp` tmpfs, liveness/readiness `/health`, PVC mount `/data/book-memo`를 포함함. 환경 변수는 Secret 값이 아닌 Secret 참조만 사용하고, 실제 Secret 생성은 수행하지 않음.

- [ ] **Step 4: 매니페스트 계약 테스트를 통과시킴.**

Run: `python3 -m unittest tests.test_k8s_book_memo_migration.BookMemoManifestTests -v`

Expected: PASS.

- [ ] **Step 5: 커밋함.**

```bash
git add infra/k8s/apps/book-memo.yaml tests/test_k8s_book_memo_migration.py
git commit -m "feat: Book Memo K3s 매니페스트 추가"
```

### Task 3: Book Memo fail-closed 전환·롤백 도구

**Files:**
- Create: `infra/k8s/tools/book-memo-cutover.sh`
- Modify: `tests/test_k8s_book_memo_migration.py`
- Modify: `docs/operations-reference.md`

**Interfaces:**
- Consumes: Compose 컨테이너 `book-memo`, 기존 데이터 디렉터리, `book-memo-data` PVC, 사전 import된 이미지.
- Produces: `--check`, `--prepare`, `--go`, `--rollback`의 상호 배타적 operator 명령.
- `--check`는 읽기 전용임. `--prepare`는 Kubernetes server dry-run과 writer 부재를 확인함. `--go`만 Compose 중지·데이터 복사·Deployment scale-up을 허용함.

- [ ] **Step 1: 상태 전이 실패 테스트를 작성함.**

```python
def test_go_requires_compose_writer_stopped_before_any_data_copy(self):
    result, calls = self.run_cutover("--go", compose_state="running")
    self.assertNotEqual(result.returncode, 0)
    self.assertIn("book_memo_cutover=FAIL", result.stderr)
    self.assertNotIn("tar", calls.read_text(encoding="utf-8"))

def test_combined_go_and_rollback_is_rejected_without_external_calls(self):
    result, calls = self.run_cutover("--go", "--rollback")
    self.assertNotEqual(result.returncode, 0)
    self.assertEqual(calls.read_text(encoding="utf-8"), "")
```

- [ ] **Step 2: 테스트가 도구 부재로 실패하는지 확인함.**

Run: `python3 -m unittest tests.test_k8s_book_memo_migration.BookMemoCutoverTests -v`

Expected: FAIL because `book-memo-cutover.sh` does not exist.

- [ ] **Step 3: fail-closed 도구를 구현함.**

```bash
case "$mode" in
  --check) assert_compose_healthy; assert_k3s_writer_absent ;;
  --prepare) assert_compose_healthy; assert_k3s_writer_absent; sudo -n k3s kubectl apply --dry-run=server -f "$MANIFEST" ;;
  --go) assert_compose_healthy; stop_compose; copy_data_once; verify_sqlite; start_k3s_writer ;;
  --rollback) stop_k3s_writer; start_compose; verify_compose_health ;;
  *) fail "exactly one mode is required" ;;
esac
```

`--go`는 이미지 반입·PVC 준비·SQLite `PRAGMA quick_check`·데이터 digest 대조를 통과해야만 수행함. 실패 시 공개 경로를 자동 변경하지 않고, Compose writer를 기존 상태로 복구한 뒤 실패 단계만 출력함. Caddy·Cloudflare Tunnel 파일은 읽거나 수정하지 않음.

- [ ] **Step 4: 전환 도구 테스트를 통과시킴.**

Run: `python3 -m unittest tests.test_k8s_book_memo_migration -v`

Expected: PASS.

- [ ] **Step 5: 운영 문서를 갱신함.**

문서에는 사전 승인, `--check`·`--prepare`·`--go`·`--rollback`의 분리, Secret 키 이름만 기록함. 데이터 경로·토큰·개인 메모·실제 공개 upstream은 기록하지 않음.

- [ ] **Step 6: 커밋함.**

```bash
git add infra/k8s/tools/book-memo-cutover.sh tests/test_k8s_book_memo_migration.py docs/operations-reference.md
git commit -m "feat: Book Memo K3s 전환 절차 추가"
```

### Task 4: 통합 검증·독립 검토·운영 승인 게이트

**Files:**
- Modify: `tests/test_documentation_index.py`
- Modify: `docs/README.md`
- Test: Task 1~3의 테스트와 관련 유지보수 묶음

**Interfaces:**
- Consumes: 이전 작업의 스크립트·매니페스트·운영 문서.
- Produces: 문서 색인과 정적 계약이 보장된 PR 준비 상태.

- [ ] **Step 1: 문서 계약 테스트를 작성함.**

```python
def test_operations_docs_index_book_memo_k3s_cutover(self):
    text = (ROOT / "docs" / "operations-reference.md").read_text(encoding="utf-8")
    self.assertIn("Book Memo K3s 전환", text)
    self.assertIn("book-memo-cutover.sh --check", text)
    self.assertIn("book-memo-cutover.sh --rollback", text)
```

- [ ] **Step 2: 실패를 확인함.**

Run: `python3 -m unittest tests.test_documentation_index.DocumentationIndexTests -v`

Expected: FAIL before docs are indexed.

- [ ] **Step 3: 문서 색인과 링크를 구현함.**

`docs/README.md`에 운영 참조 문서 링크를 추가하고, 전환 문서에는 공개 URL이나 비밀값을 포함하지 않음.

- [ ] **Step 4: 관련 검증을 실행함.**

Run:

```bash
python3 -m unittest tests.test_k3s_app_image_transfer tests.test_k8s_book_memo_migration tests.test_documentation_index -v
python3 tests/run_service_tests.py --suite maintenance
git diff --check
```

Expected: 모두 PASS, 공백 오류 없음.

- [ ] **Step 5: 변경 하네스와 독립 검토를 수행함.**

```bash
git diff --name-status -z --find-renames origin/main HEAD > /tmp/k3s-app-migration-paths.z
python3 scripts/run_change_harness.py --input /tmp/k3s-app-migration-paths.z --input-format git-name-status-z --agent-context
python3 scripts/run_change_harness.py --input /tmp/k3s-app-migration-paths.z --input-format git-name-status-z --check-result maintenance=success --agent-context
```

독립 검토는 금지 영역(Portal·Caddy·Tunnel·Secret 값), 데이터 동시 쓰기 방지, `--go` 승인 게이트, 롤백 순서를 확인함.

- [ ] **Step 6: PR 준비 커밋을 생성함.**

```bash
git add tests/test_documentation_index.py docs/README.md
git commit -m "docs: Book Memo K3s 전환 기준 추가"
```

## 실행 순서와 분업

| 역할 | 담당 범위 | 같은 파일 동시 수정 금지 |
|---|---|---|
| 주 에이전트 | 작업 상태, 전환 경계, N100 승인 게이트, 통합 검증 | `infra/k8s/tools/book-memo-cutover.sh` |
| 구현 에이전트 | Task 1~3을 순차 구현 | 주 에이전트와 동일 파일을 동시에 수정하지 않음 |
| 독립 검토 에이전트 | diff·테스트·금지 영역·데이터 안전성 검토 | 구현 파일 수정 금지 |
| 운영 검토 에이전트 | N100 반입·K3s·공개 health·롤백 순서 검토 | 실제 적용 전 읽기 전용 확인만 수행 |

실제 N100 반입은 Task 4의 모든 검증과 독립 검토가 통과한 뒤 사용자에게 서비스별 승인을 받아 별도 단계에서 수행함. 자동 배포 대상이 아니므로 push·병합 자체를 운영 적용으로 표현하지 않음.

## 계획 자체 점검

| 설계 요구 | 대응 작업 | 결과 |
|---|---|---|
| 맥북 AMD64 빌드·N100 반입 | Task 1 | 포함됨 |
| K3s Deployment·Service·PVC | Task 2 | 포함됨 |
| 데이터 동시 쓰기 방지·롤백 | Task 3 | 포함됨 |
| 테스트·문서·독립 검토 | Task 4 | 포함됨 |
| 서비스별 순차 전환 | Task 1~4 후 다음 서비스 별도 계획 | 포함됨 |
| Caddy·Tunnel·Portal 제외 | Global Constraints, Task 3~4 | 포함됨 |

구현 미정 표기는 사용하지 않았으며, 실제 StorageClass·데이터 크기·공개 upstream은 구현 전 N100 읽기 전용 사전 점검 항목으로 유지함.
