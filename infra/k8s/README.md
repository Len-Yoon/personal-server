# K3s 운영

N100의 K3s는 현재 Portal·뉴스·YouTube Memo·Book Memo와 모니터링 운영에 사용함. 이 문서는 실제 운영 도구의 진입점만 정리하며, Secret 값·비밀번호·token은 출력하거나 문서화하지 않음.

## 정기 운영 실행 경계

| 구분 | 주기 | 실행 목적 | 월간 감사와의 관계 |
|---|---:|---|---|
| 공개 상태 감시 | 약 5분 | 외부에서 공개 health 장애·복구를 신속히 감지함 | GitHub Actions에서 독립 실행하며 월간 감사로 대체하지 않음 |
| 일별 SLO 증적 | 매일 02:15, 운영 활성·최근 수집 성공 | Prometheus 직전 24시간과 공개 health 교차 확인 증적을 최근 30건 보관함. 세부 건강 항목은 `failed`일 수 있음 | 월간 감사가 고정 ConfigMap을 읽기 전용으로 집계함 |
| Portal PVC 백업·복원 검증 | 매일 03:00 | 최신 복구 가능 증적을 유지하고 백업 실패를 조기에 감지함 | 별도 CronJob이 실행하며 월간 감사는 증적만 읽기 확인함 |
| 내부 SRE 통합 점검 | 매월 1일 03:30 | Portal·K3s·백업 증적·격리 복구 훈련을 한 번에 확인함 | `monthly-sre-audit`만 활성화함 |
| 코드 변경 검증 | 변경 시점 | 변경 영향 범위의 회귀를 병합 전 확인함 | 정기 운영 점검과 별도임 |

기존 `quarterly-sre-audit` 및 validation CronJob은 이력·rollback 검증용으로 suspended 상태를 유지함. 동일한 내부 점검을 별도 주기로 중복 실행하지 않음.

## 현재 구성

| Namespace | 구성 | 역할 |
|---|---|---|
| `personal-server` | `portal-web`, `crawler-worker`, `youtube-memo`, `book-memo`, Service, 서비스별 PVC | Portal·파일함·관리자·포트폴리오·뉴스·메모 |
| `monitoring` | Prometheus, Grafana, Alertmanager, SRE Telegram relay | 상태 수집·시각화·경고 전달 |

Portal은 K3s PVC를 상태 저장소로 사용하며, Compose `portal-web`은 동시에 실행하지 않음. Caddy는 `host.docker.internal:30080` NodePort를 통해 K3s Portal로 전달함. K3s runtime에서는 `.env`의 `PORTAL_UPSTREAM=host.docker.internal:30080` 설정이 필요하며, Compose runtime에서는 이 값을 `portal-web:8000`으로 유지하거나 비워 Compose 기본값을 사용함. Portal cutover는 이 값을 자동으로 전환하므로 수동 변경 대신 해당 절차를 사용함.

2026-09-27 기준 Crawler·Book·YouTube·Portal의 A2·A4·A5 이미지 적용과 외부 health 검증은 [최신 고도화 적용 결과](../../docs/20260921_프로젝트보완_개발계획.md#2026-09-27-a2a4a5-고도화-및-n100-적용-결과)를 따름. 2026-09-28 Book·YouTube·Crawler PVC 백업 CronJob은 수동 백업·격리 복원 성공 뒤 [자동 실행을 활성화](../../docs/reviews/20260928_서비스별_PVC_백업_자동실행_적용결과.md)함. 그러나 같은 날 13:00 뉴스 백업의 첫 정기 Job은 사전 점검에서 실패했으며, 원인과 수정 후보·미적용 상태는 [운영 반영 사전검토](../../docs/reviews/20260928_Book_SLO_뉴스백업_운영반영_사전검토.md)에 기록함. 뉴스 10차의 과거 보류와 후속 적용은 각각 [당시 K3s 앱 배포 결과](../../docs/reviews/20260921_K3s앱배포_검증결과.md)와 [뉴스·SLO 운영 적용 결과](../../docs/reviews/20260922_뉴스_SLO_운영적용_검증결과.md)에 기록됨. 초기 manifest의 `replicas: 0`·sentinel image는 현재 운영 상태를 나타내지 않으므로 재적용하지 않음.

### 서비스별 PVC 백업 운영 목표 상태

Book·YouTube·Crawler 백업 CronJob의 맥 저장소 기준 운영 상태는 [production-cronjob-state.json](backup-automation/production-cronjob-state.json)에 기록함. 세 `*-pvc-backup-cronjob.yaml`은 최초 설치용 `suspend: true`·sentinel 이미지이므로 활성 운영 CronJob에 전체 적용하지 않음. 목표 파일은 일정·시간대·시작 마감·활성 상태·고정 이미지 digest를 보관하며 Secret 값은 포함하지 않음.

2026-09-28 최종 승인에 따라 이 목표 상태를 N100에서 [검증](../../docs/reviews/20260928_백업운영목표상태_동기화결과.md)함. 세 CronJob이 이미 목표와 같아 적용 명령은 쓰기 0건으로 종료됨. 이후 변경에도 아래 절차와 최종 운영 승인 기준을 유지함.

맥에서 변경·테스트 → 기능 브랜치 PR·CI → `main` 병합을 마친 뒤, **N100 운영 적용은 별도 최종 승인**을 받고 정확한 커밋으로 fast-forward함. N100 WSL의 저장소 루트에서 아래 순서로 확인함. `--check`는 읽기 전용이며 목표와 실제 값이 다르면 실패함. `--apply`는 사전 승인된 대상 커밋과 깨끗한 추적 작업공간을 요구하고, 세 CronJob의 `startingDeadlineSeconds`와 `suspend`만 조건부 patch함. 이미지가 목표와 다르면 이미지 반입·교체 절차를 별도로 검토하며 이 도구로 수정하지 않음.

```bash
python3 infra/k8s/tools/service-pvc-backup-production-state.py --validate
python3 infra/k8s/tools/service-pvc-backup-production-state.py --check
# 최종 운영 승인 뒤에만 실행함. 승인된 main의 40자리 전체 SHA를 입력함.
python3 infra/k8s/tools/service-pvc-backup-production-state.py --apply --expected-sha "$APPROVED_MAIN_SHA"
python3 infra/k8s/tools/service-pvc-backup-production-state.py --check
```

도구는 Job 실행 중·writer/PVC/백업 증적/Relay 이상·예정 시각 직후 300초·이미지 미반입·동시 수정 충돌을 차단함. JSON Patch의 UID·resourceVersion 조건과 server dry-run 뒤 실제 적용하며, 이미 일치하면 쓰기 0건임. 여러 대상 중 일부만 적용된 뒤 실패하면 재실행이나 rollback을 자동으로 하지 않고 실제 상태를 다시 조회함. 적용 후 Portal 및 변경 서비스의 외부 health를 10초 간격 3회 확인하고, 다음 정기 실행의 성공은 별도 증적으로 검증함. 이 도구는 Secret·PVC·ConfigMap·RBAC·Deployment를 쓰지 않음.

`--check`는 목표 필드와 백업 명령·ServiceAccount·PVC/Secret mount·핵심 보안 설정을 확인함. 2026-09-28 뉴스 백업 실패 보완으로 목표 이미지 digest를 먼저 갱신했으므로 승인된 이미지 반입·교체 전에는 이미지 불일치로 실패하는 것이 정상임. CronJob 전체 spec·Secret 내용·이미지의 플랫폼 manifest까지 포괄 감사하는 도구는 아니므로 운영 사전검토에서 별도 확인함. N100 이미지 목록에 고정 alias가 있어도 해당 이미지가 `linux/amd64`로 실행 가능한지는 정기 Job 또는 별도 이미지 검사로 확인 필요함.

### 새벽 순차 백업 전환 목표

맥 저장소의 [순차 백업 설계](../../docs/superpowers/specs/2026-09-29-sequential-night-backup-design.md)는 네 백업을 서울 시각 00:30에 Portal → Book Memo → YouTube Memo → News Hub 수집기 순서로 실행하도록 정의함. 앞 Job의 최종 종료를 확인한 직후 다음 Job을 시작하며, 실패해도 최종 종료가 확인되면 다음 백업을 시도함. 종료 상태가 불명확하면 중복 백업을 피하기 위해 중단함. 네 개별 CronJob은 중지하고 기존 jobTemplate을 순차 실행에 재사용함. 이 절은 **운영 적용 목표**이며 N100 timer 활성화 또는 첫 야간 백업의 성공을 의미하지 않음.

운영 전환에는 Relay의 순차 실행 상태 감시 적용, 기존 네 CronJob 중지, `pvc-backup-sequence-state` ConfigMap 최초 생성, N100 사용자 systemd 단위 설치·활성화가 필요함. 상태 ConfigMap은 한 번 생성한 뒤 다시 manifest로 적용하면 기존 실행 기록이 초기화될 수 있으므로 재적용하지 않음. 기존 Book·YouTube·Crawler 목표는 위 전용 도구로 제한 적용함. Portal은 현재 CronJob의 UID·resourceVersion·기존 schedule·jobTemplate을 읽기 전용으로 확인한 뒤, UID·resourceVersion 일치 조건의 JSON patch로 `startingDeadlineSeconds: 300`과 `suspend: true`만 변경함. 기존 active Job이 없고 writer·잠금 상태가 정상인지 먼저 확인하며 설치용 전체 manifest는 재적용하지 않음. installer의 `--preflight`는 사용자 systemd·linger·K3s 접근을 확인함. `--activate`는 네 CronJob 중지·시작 마감 300초·기존 Portal timer 및 service 비활성·활성 백업 Job 부재·현재 네 백업 템플릿의 보안 계약·호스트 잠금 획득 가능성을 확인한 뒤 timer를 켬.

```bash
bash infra/k8s/tools/pvc-backup-sequence-automation.sh --preflight
bash infra/k8s/tools/pvc-backup-sequence-automation.sh --install
# 네 CronJob 중지·Relay 적용·상태 ConfigMap 최초 생성 후, 최종 운영 승인 범위에서만 실행함.
bash infra/k8s/tools/pvc-backup-sequence-automation.sh --activate
bash infra/k8s/tools/pvc-backup-sequence-automation.sh --status
```

timer는 놓친 시각의 낮 시간 보충 실행을 하지 않음. 조정기는 06:00 이후 새 Job을 시작하지 않지만 이미 시작한 Job은 종료까지 관찰함. 다음 실제 00:30 실행에서 순서·네 복원 검증·Relay 알림을 별도 확인해야 함. 되돌릴 때 `--deactivate`는 먼저 다음 timer 실행을 막음. 실행 중인 service/Job 또는 미완료 상태가 있으면 상태 기록 변경을 거부하므로 종료·결과 확인 뒤 다시 실행해야 명시적 중지 시각이 기록됨. Relay는 중지 후 새 날짜의 실행 누락 감시를 멈추되 이미 시작된 실행의 확정 실패 알림은 유지하며, 활성화 후 상태 ConfigMap 자체가 삭제·초기화된 경우에는 상태 누락을 알림. Book·YouTube·Crawler의 운영 목표 파일도 기존 `suspend: false`로 되돌려 맥에서 검증·반영한 뒤 전용 도구로 조건부 적용하고, Portal은 시작 마감 300초를 유지한 채 UID·resourceVersion 조건부 patch로 재활성화함. 실행 중 Job을 삭제하거나 두 예약 방식을 동시에 활성화하지 않음. 상태 ConfigMap이 `completed`가 아니어서 제한 적용 도구가 차단하면 재실행·강제 변경하지 않고 Job·writer·잠금·증적을 확인한 뒤 별도 수동 조건부 복구 경로를 검토함.

## 빠른 상태 확인

```bash
sudo k3s kubectl get nodes
sudo k3s kubectl -n personal-server get deploy,pod,pvc
sudo k3s kubectl -n monitoring get pod,pvc
bash infra/k8s/tools/sre-health-audit.sh
```

## Grafana와 Prometheus

Grafana는 N100 내부 전용임. 필요할 때 아래 명령을 실행한 뒤 브라우저에서 `http://127.0.0.1:3000`을 열고, 종료할 때 `Ctrl+C`를 누름.

```bash
sudo k3s kubectl -n monitoring port-forward --address 127.0.0.1 service/personal-server-monitoring-grafana 3000:80
```

설치 상태를 점검할 때는 다음을 사용함.

```bash
bash infra/k8s/tools/monitoring-preflight.sh
bash infra/k8s/tools/monitoring-verify.sh
```

`monitoring-install.sh --apply`와 제거 명령은 운영자 승인 후에만 실행함. Grafana와 Prometheus PVC는 기본 제거에서 보존함.

## Portal HTTP Grafana 대시보드

Portal HTTP 관측성 대시보드는 `monitoring` namespace의 `portal-http-observability` ConfigMap으로 관리함. 대시보드에는 요청 수, HTTP 상태 코드별 요청 수, 5xx 비율, p95 응답 시간이 포함됨.

적용 전에는 운영자가 Prometheus에서 `portal_http_requests_total`과 `portal_http_request_duration_seconds` 수집 여부를 수동 확인 필요함. 전용 도구는 Grafana Deployment와 고정 dashboard manifest만 검증하며, datasource·메트릭 존재 여부를 자동으로 확인하지 않음. 적용·상태 확인·롤백은 아래 전용 도구만 사용함.

```bash
bash infra/k8s/tools/monitoring-dashboard-apply.sh --check
bash infra/k8s/tools/monitoring-dashboard-apply.sh --apply
bash infra/k8s/tools/monitoring-dashboard-apply.sh --rollback
```

`--apply`는 대시보드 ConfigMap만 적용하며, `--rollback`은 해당 ConfigMap만 제거함. Secret, Portal PVC, Caddy 설정, Portal runtime Deployment는 변경하지 않음.

적용 후에는 Grafana 포트포워드를 실행하고 `http://127.0.0.1:3000`에서 `Portal HTTP 관측성` 대시보드가 로드되는지 확인함. 포트포워드는 확인이 끝난 뒤 `Ctrl+C`로 종료함.

```bash
sudo k3s kubectl -n monitoring port-forward --address 127.0.0.1 service/personal-server-monitoring-grafana 3000:80
```

## Telegram SRE 알림

Alertmanager 경고는 `sre-telegram-relay`를 통해 Telegram으로 전달함. Secret 값은 N100의 승인된 Secret 관리 절차로만 관리함.

- 승인된 bearer 값은 runtime Secret 키 `alertmanager_auth_token`에만 입력함.
- 임시 Alertmanager 설정 파일에는 `credentials_file` 경로만 유지하며 bearer 값은 포함하지 않는다.

```bash
bash infra/k8s/tools/sre-telegram-preflight.sh --alertmanager-config-file <0600-설정파일>
bash infra/k8s/tools/sre-telegram-verify.sh
```

relay, PrometheusRule, RBAC 경계, Prometheus target 상태를 검증하며 Secret 값은 읽지 않음.

## Portal PVC 백업

Portal PVC 백업은 N100의 K3s CronJob으로 운영 중임. 2026-09-27 후속 조회에서 최신 Job 성공과 `portal_pvc_backup=PASS` 증적을 확인했고, 원격 암호문 전체의 SHA-256 재계산 결과가 증적과 일치함. 이 확인은 원격 파일의 무결성 검증이며 공유·접근 권한 감사는 포함하지 않음. 기존 사용자 timer는 비활성 상태로 운영함. 아래는 최초 설치·재설치 절차이며, 초기 CronJob은 `suspend: true` 상태로 배포됨. 새 환경에서는 승인된 Secret Manager 또는 SOPS/age 절차로 사전 시딩된 runtime Secret과 runner image가 준비되기 전에는 활성화하지 않음. 저장소 도구는 Secret 값·rclone 설정·age identity를 생성·입력·출력하지 않음.

백업 도구는 생성한 archive를 `age`로 암호화하고 암호문 파일의 SHA-256을 증적의 `artifact_digest`로 기록함. SHA-256은 암호화 방식이나 복호화 가능성을 뜻하지 않으며, 일일 Job은 원격에서 내려받은 암호문의 해시와 격리 복원 결과도 확인함. 2026-09-27 후속 검사는 별도로 원격 바이트를 다시 읽어 기록된 해시와 비교한 것임.

전환 전에는 아래 읽기 점검과 client-side render를 실행함. `--preflight`는 Secret **이름과 key 이름만** 확인하고, Portal PVC가 `Bound`·`ReadWriteOnce` mount 계약을 충족하는지 확인함.

```bash
bash infra/k8s/tools/portal-pvc-backup-cronjob.sh --preflight
bash infra/k8s/tools/portal-pvc-backup-cronjob.sh --render
bash infra/k8s/tools/portal-pvc-backup-verify.sh --check
```

운영자 승인 후 `--apply`는 suspended CronJob과 최소 RBAC만 적용함. 기존 systemd timer를 비활성화하고, 수동 실행의 백업·복원 검증 및 Telegram 결과를 확인한 뒤에만 `--activate`로 CronJob을 해제함. 두 scheduler가 동시에 활성화되는 상태는 허용하지 않음.

```bash
bash infra/k8s/tools/portal-pvc-backup-cronjob.sh --apply
bash infra/k8s/tools/portal-pvc-backup-cronjob.sh --status
# 기존 timer가 inactive이고 수동 검증이 성공한 뒤에만 실행함.
bash infra/k8s/tools/portal-pvc-backup-cronjob.sh --activate
```

CronJob은 매일 03:00 KST에 실행되며, `Forbid` 동시 실행 제한·실패 재시도 없음·read-only PVC mount·고정 ServiceAccount 권한을 사용함. 성공·변경 없음·실패·복원 검증 실패는 Telegram SRE relay로 상태 전환을 전달함. 실행 중 백업이 중단되면 300초 종료 유예 안에서 Portal replica 복구를 시도하며, 복구 상태를 확인해야 함.

## 일별 SLO 증적

`slo-daily-evidence` CronJob은 서울 기준 매일 02:15에 실행하도록 정의됨. 2026-09-22에 freshness 다중 시계열 집계 수정 collector를 반입하고 수동 Job 1회 검증을 완료했으며, 수동 검증 뒤 별도 승인으로 N100 CronJob을 `suspend: false`로 활성화함. 당일 증적은 `ok`로 검증됐음. [SLO 원인 분석](../../docs/reviews/20260921_SLO증적실패_원인분석.md)과 [운영 적용 검증 결과](../../docs/reviews/20260922_뉴스_SLO_운영적용_검증결과.md)를 참조함. `monitoring/slo-daily-evidence` ConfigMap의 `records.json`에 날짜별 검증 기록을 최대 30건 보관함. 같은 날짜의 재수집은 해당 기록을 교체함. Prometheus retention은 변경하지 않으며, 결측·질의 오류는 `unobservable`로 기록하고 Job이 실패함. 공개 health 비-200은 `failed`로 기록하며 관측 자체가 성공했다면 Job 실패로 취급하지 않음.

2026-09-28 읽기 점검에서 Job은 증적 수집에 성공했으나 공개 health·Portal Ready·뉴스 freshness 세부 값은 `failed`였음. [사전검토](../../docs/reviews/20260928_Book_SLO_뉴스백업_운영반영_사전검토.md)의 새 이미지는 앞으로 공개 health의 비-200 상태 코드만 Job 로그에 남기며, 기존 판정과 증적은 바꾸지 않음. 과거 실패 원인은 확인 필요임.

실행 권한은 고정 증적 ConfigMap의 `get`, `patch`로 제한함. CronJob은 동시 실행 금지, 재시도 없음, 최대 180초 실행, non-root, read-only root filesystem, capability 전체 제거 및 크기가 제한된 `/tmp`만 사용함. Secret·PVC·host volume을 mount하지 않으며 Portal·K3s 복구 또는 배포 차단을 수행하지 않음.

다음은 운영자 승인 후 N100에서 수행할 적용 순서임. 저장소 병합만으로 운영 적용 또는 자동 실행이 완료되지 않음.

1. 맥 저장소에서 먼저 테스트하고 Linux AMD64 OCI archive를 빌드함. archive SHA-256과 OCI manifest digest·플랫폼을 대조한 뒤 최종 운영 승인 후 N100에 전송·검증·반입함. 2026-09-28 후보의 이미지 참조와 해시는 [사전검토](../../docs/reviews/20260928_Book_SLO_뉴스백업_운영반영_사전검토.md)를 따름. N100에서 소스를 새로 빌드하지 않음.

   ```bash
   # 맥: 변경한 소스와 Dockerfile을 함께 고정한 linux/amd64 OCI archive 생성
   docker buildx build --provenance=false --platform linux/amd64 \
     --tag personal-server-slo-evidence:20260928-http-status \
     --file infra/k8s/slo-evidence/Dockerfile \
     --output type=oci,dest=data/build-artifacts/20260928-book-slo-news/slo-evidence-20260928-http-status.oci.tar,annotation-manifest-descriptor.io.personal-server.image-ref=personal-server-slo-evidence:20260928-http-status .
   ```

2. 최초 설치 시에만 전체 manifest를 적용함. `monitoring/slo-daily-evidence`가 이미 존재하면 `records.json: []`가 포함된 ConfigMap을 다시 적용하지 않음. **기존 활성 CronJob 갱신에는 설치용 `suspend: true` manifest도 재적용하지 않고 이미지 필드만 현재 값 일치 조건으로 변경함.** 기존 증적과 일정·활성 상태를 보존하고, 실행 중인 Job이 있으면 종료 확인 전 변경·수동 재실행하지 않음. 조회 실패를 리소스 부재로 취급하지 않음.

   ```bash
   # 최초 설치이며 증적 ConfigMap이 없음을 확인한 경우에만 실행함.
   sudo k3s kubectl apply -f infra/k8s/slo-evidence/slo-daily-evidence-cronjob.yaml
   sudo k3s kubectl -n monitoring get cronjob slo-daily-evidence -o jsonpath='{.spec.suspend}'
   ```

3. **최초 설치 시에만** `suspend=true`를 확인함. 기존 활성 CronJob의 이미지 갱신은 `suspend=false`를 보존함. 두 경우 모두 실행 중인 Job이 없음을 확인한 뒤 고유 실행 이름으로 수동 Job을 한 번 생성함. 명령 응답이 유실되면 같은 생성 명령을 반복하지 않고 Job 상태부터 읽기 확인함. 수동 Job 생성은 CronJob의 `Forbid` 제한을 적용받지 않음.

   ```bash
   slo_job="slo-daily-evidence-manual-$(date +%s)"
   sudo k3s kubectl -n monitoring create job "$slo_job" --from=cronjob/slo-daily-evidence
   sudo k3s kubectl -n monitoring wait --for=condition=complete "job/$slo_job" --timeout=240s
   ```

4. Job 성공 및 이번 실행 날짜의 증적 존재를 확인함. ConfigMap은 원문 출력 없이 수집기의 `validate_record`로 고정 필드·자료형·UTC 시각·수치 범위를 검증하고, 날짜 중복 없음·최대 30건·날짜 내림차순도 확인함. 실패 또는 관측 불가 결과는 성공으로 보정하지 않음. 기존 활성 CronJob 갱신에서 수동 검증이 실패하면 추가 변경을 중단하고 실제 상태를 조회하며, 기존 자동 실행의 중단 여부는 별도 판단함.
5. **최초 설치 시에만** 수동 Job 성공과 증적 검증 뒤 일일 자동 실행을 활성화함. 기존 활성 CronJob 갱신에서는 활성 상태를 변경하지 않음. 외부 health는 `https://len.pe.kr/health`를 10초 간격으로 3회 호출해 모두 HTTP 200인지 확인함. 명시적으로 자동 수집을 중단하기로 결정한 경우에만 CronJob을 suspend하며 증적 ConfigMap은 삭제하지 않음.

   ```bash
   sudo k3s kubectl -n monitoring patch cronjob slo-daily-evidence --type=merge -p '{"spec":{"suspend":false}}'
   # 자동 수집 중단이 필요한 경우에만 실행함.
   sudo k3s kubectl -n monitoring patch cronjob slo-daily-evidence --type=merge -p '{"spec":{"suspend":true}}'
   ```

월간 감사는 30건 미만의 초기 수집 구간을 `unobservable`로 보고함. 이 증적은 일별 집계이며 기존 약 5분 공개 상태 감시를 대체하지 않음. 실제 적용 시 저장소 병합·N100 동기화·이미지 반입·수동 검증·자동 실행 활성화·외부 health 검증을 각각 구분하여 보고함.

## 월간 SRE 통합 점검 자동화

월간 SRE 통합 점검은 N100에서 활성화됨. `monthly-sre-audit`는 매월 1일 03:30의 유일한 내부 정기 점검 실행기이며, Portal 상태·백업 증적·격리 Pod 복구 단계와 기존 SRE Telegram relay 전달을 확인함. 이는 일일 백업·복원 검증 및 약 5분 간격 공개 상태 감시를 대체하지 않음. 저장소 병합만으로는 CronJob이 활성화되지 않으며, 이후 재설치 또는 변경 적용 전에도 아래 `--preflight`와 `--render`를 먼저 통과해야 함. 설치 과정에서 Secret·token·Telegram chat ID·rclone 자격 증명을 생성·복제·읽지 않음. 백업 단계는 CronJob이 기록한 `personal-server/portal-pvc-backup-evidence` ConfigMap만 fail-closed로 검증함. 공개 상태 감시는 GitHub Actions에서 독립 유지하며 이 CronJob으로 통합하지 않음.

```bash
bash infra/k8s/tools/quarterly-sre-audit-automation.sh --preflight
bash infra/k8s/tools/quarterly-sre-audit-automation.sh --render
# 재설치 또는 운영 변경은 별도 승인 후 N100에서 실행함.
bash infra/k8s/tools/quarterly-sre-audit-automation.sh --install
bash infra/k8s/tools/quarterly-sre-audit-automation.sh --status
```

`--install`은 host 단일 실행 lock을 먼저 획득한 뒤 아래 순서로만 수행함.

1. preflight와 client-side render를 수행하고, `monitoring` namespace의 모든 `monthly-sre-audit-*`, `quarterly-sre-audit-*` Job이 `Complete=True` 또는 `Failed=True` 종료 condition을 가진 상태인지 확인함. Job 목록 조회 오류 또는 종료 미확정 Job이 있으면 manifest를 적용하지 않고 중단하며, 이 단계에서는 기존 CronJob schedule·상태를 변경하지 않음.
2. 새 월간 CronJob, 기존 분기·validation CronJob의 `suspend: true` 상태와 최소 권한 RBAC를 적용하고 suspended 상태를 확인함.
3. `monitoring/sre-telegram-quarterly-audit-status` ConfigMap이 없을 때만 비밀값 없는 빈 결과 필드를 생성함. 기존 ConfigMap 데이터는 덮어쓰지 않음.
4. 기존 사용자 `personal-server-quarterly-sre-audit.timer`가 존재하면 `disable --now`로 중지·비활성화함. 이어서 legacy `personal-server-quarterly-sre-audit.service`가 실행 중이 아닌지 확인함. 서비스 상태가 `inactive` 또는 `failed`인 경우에도 `MainPID=0`일 때만 통과시키며, 실행 중이거나 전환 중인 상태 또는 `MainPID`가 0이 아니면 강제 종료하지 않고 설치를 차단함.
5. 수동 Job 생성 직전에 월간·기존 분기·validation Job의 종료 condition을 다시 확인함. 재시도 시 이전 실행의 종료가 확정되지 않았거나 상태 조회가 불확실하면 두 번째 Job을 생성하지 않음.
6. 새 월간 CronJob에서 고유 이름의 수동 Job을 생성하고 완료 성공을 대기함.
7. `monitoring/sre-telegram-quarterly-audit-status`의 기존 `status=passed` 계약과 relay 전달을 확인한 뒤에만 새 월간 CronJob suspend를 해제함. 기존 분기·validation CronJob은 suspended 상태를 유지함.

lock 경합, Job 목록 조회 오류 또는 종료 미확정 Job으로 manifest 적용 전 차단되면 기존 CronJob schedule·상태는 변경하지 않음. suspended CronJob 적용 이후 legacy service 활성, 수동 Job 실패, 상태 ConfigMap 조회 실패 또는 상태 미확인으로 차단되면 CronJob은 suspended 상태를 유지함. 기존 systemd service·timer template은 이력 보존 목적으로만 남아 있으며, 신규 설치 또는 실행 경로에서 설치·사용하지 않음.

새 월간 CronJob은 `Asia/Seoul` 기준 매월 1일 03:30에 1회 실행되며, `Forbid` 동시 실행 제한과 실패 재시도 없음 조건을 사용함. 이전 분기·validation CronJob은 rollback·검증 이력용으로 suspended 상태를 유지하며, 월간 점검과 같은 검증을 자동으로 중복 실행하지 않음. runner는 다음 세 점검을 수행하고 어느 한 단계라도 실패하면 결과를 fail-closed로 보고함.

| 점검 | 실행 도구 | 범위 |
|---|---|---|
| K3s·Portal 상태 | Kubernetes API | Node와 `personal-server/portal-web` Deployment 상태 확인 |
| 백업 증적 검증 | `portal-pvc-backup-evidence` ConfigMap | K3s PVC 백업·복원 증적의 유효성·만료 상태를 fail-closed로 확인 |
| 격리 Pod 복구 훈련 | `sre-recovery-lab/sre-pod-recovery` | 재시작 횟수 증가와 Ready 복구 확인 후 scale 성공 및 Deployment의 `.spec.replicas=0`으로 정리 요청을 검증함 |

정리 요청 이후 Pod 종료는 Kubernetes가 비동기로 처리함. runner는 Pod 소멸을 기다리지 않으며, 정상 종료 유예 중 Pod가 남아 있는 상태를 점검 실패로 판정하지 않음. scale 또는 Deployment 조회 실패, `.spec.replicas`가 정확히 `0`이 아닌 경우에는 복구 실습과 종합 결과를 실패로 기록함.

이 과정은 Portal·Caddy·Cloudflare Tunnel·Compose 서비스를 stop, restart, scale 또는 rollout하지 않으며, 운영 데이터와 Portal PVC를 변경하지 않음. Compose 컨테이너 상태는 Docker socket·hostPath 접근이 필요한 별도 운영 증적으로 관리하며, 월간 CronJob 결과에 포함하지 않음. 결과는 실행 ID, 완료 시각, 종합 상태와 세 단계 상태만 기존 `monitoring/sre-telegram-quarterly-audit-status` ConfigMap에 기록함. 기존 `sre-telegram-relay`는 Telegram 성공 응답이 확인될 때까지 재시도하며, 응답 유실 시 드물게 중복 메시지가 수신될 수 있음. 명령 출력 전문·namespace/Pod 식별자·파일 경로·내부 IP·Secret 값은 전달하지 않음.

실제 적용 시 운영자는 `--install`이 생성한 수동 Job 1회가 성공한 뒤 다음 세 가지를 모두 직접 확인해야 함.

1. ConfigMap 기록에 세 단계 결과와 종합 결과가 남았는지 확인함.
2. 고정 `sre-recovery-lab/sre-pod-recovery` Deployment의 `.spec.replicas=0`으로 정리 요청이 반영되었는지 확인함. Pod는 정상 종료 유예 동안 남을 수 있으며, Kubernetes가 비동기로 종료함. 고정 namespace와 Deployment 자체는 삭제하지 않음.
3. 기존 SRE Telegram relay를 통해 요약 메시지가 수신되었는지 확인함.

Telegram 수신을 확인하기 전에는 월간 점검 적용 또는 알림 정상으로 판단하지 않음. `--status`는 새 월간 CronJob의 suspend 상태와 ConfigMap의 `run_id`, `status`, `completed_at`, `health_audit`, `backup_check`, `recovery_lab`만 출력하므로 Secret 값은 포함하지 않음. `completed_at`은 서울 기준 `YYYY-MM-DD HH:MM`으로 표시됨. 수동 Job 직후에는 `run_id`와 완료 시각이 해당 실행 결과인지 확인 필요함.

```bash
bash infra/k8s/tools/quarterly-sre-audit-automation.sh --status
```

## Portal 전환과 복구

Portal 전환은 명시적 운영 작업임. 백업 증거를 먼저 확인하고, 전환과 공개 경로 변경을 분리해 실행함.

```bash
bash infra/k8s/tools/portal-backup-verify.sh
bash infra/k8s/tools/portal-cutover.sh --go
bash infra/k8s/tools/portal-cutover.sh --switch-caddy
```

실패하면 실행기가 Compose Portal과 이전 공개 경로를 복구하도록 설계됨. 결과는 마지막 `portal_cutover=PASS|FAIL`로 판단함.

## SRE Pod 자동복구 실습

운영자 전용 실습은 `sre-pod-recovery-lab.sh`로 실행하며, production Portal과 별도의 namespace를 사용함.
실습 리소스는 sre-recovery-lab-<run-id> namespace에만 생성된다.

```bash
bash infra/k8s/tools/sre-pod-recovery-lab.sh --run
bash infra/k8s/tools/sre-pod-recovery-lab.sh --cleanup <run-id>
```

성공 결과에는 liveness 실패 뒤 `restartCount`가 증가한 사실과 Pod가 `Ready` 조건으로 복구됨이 포함됨.
이 실습은 다른 namespace, Portal, Compose, Caddy, scheduler를 변경하거나 재시작하지 않는다.
운영 대상은 변경하지 않는다.

## 운영 경계

- Secret 생성·값 입력·출력은 이 저장소 도구의 범위 밖임.
- Portal PVC·K3s·Caddy·Cloudflare Tunnel은 N100 안전 자동 배포 대상이 아님.
- 임시 Pod 자동복구 실습은 `sre-pod-recovery-lab.sh`만 사용하며 production Portal을 변경하지 않음.

## Portal 보안 smoke와 전환 경계

`portal-secret-shadow-smoke.sh`는 `isolated manual smoke` 절차이며, 운영 Portal과 분리된 임시 namespace에서만 실행함. 이 smoke는 `optional HomeOps/portfolio` 설정, `data copy`, `Caddy routing`, `actual cutover`을 검증하지 않음. 실제 데이터 이동과 공개 경로 전환은 별도 승인·백업·복구 검증이 필요한 운영 작업임.

Portal cutover는 `operator-only` 절차임. 실행 전 `K3s Secret encryption`과 `backup evidence`를 확인하고, `Compose writer` 단일 소유권 및 `PORTAL_BACKUP_MAX_AGE_SECONDS=86400` 유효성을 검증함. 데이터 매니페스트는 `sha256`으로 비교하고, 임시 Secret 파일은 `0600`으로 제한함. 공개 경로 변경과 정리는 별도 호출함.

```bash
bash infra/k8s/tools/portal-cutover.sh --rollback-caddy
bash infra/k8s/tools/portal-cutover.sh --cleanup-rolledback
```
