# Grafana WSL CPU·메모리 수집 검증

## 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 기준 | kube-prometheus-stack 88.6.1 / node-exporter 차트 4.56.1 | 기존 release 전체 upgrade 제외 |
| 목적 | 단일 Cluster의 CPU·메모리 사용률 결측 복구 | Windows 물리 호스트가 아닌 WSL2 노드 범위 |
| 승인 | 실제 WSL 검증·운영 적용·문제 시 원복과 결과 확인 | 이번 수집기 추가에 한정 |
| 추가 승인 | 고정된 공식 개발 이미지로 시험 후 적용 | 안정 이미지의 신규 HIGH 15건 발견 후 별도 수용 |

## 핵심 요약

공유 host root 마운트를 제거하고 CPU·meminfo collector만 활성화함. hostNetwork·hostPID·hostIPC를 사용하지 않고 `/proc`·`/sys`만 읽기 전용으로 제공함. 현재는 구현·오프라인 검증 단계이며 실제 WSL 시험·운영 적용 결과는 아래 절과 해당 PR에 확정 기록함.

## 상세 내용

| 대상 | 설정·동작 | 비고 |
|---|---|---|
| 이미지 | 공식 `master-distroless`의 고정 index `sha256:2aeca551ca3cb8c90f11fee9bcdf9859725c48f272d478aa79b9bbc48eda13ed` | 정식 릴리스 전 이미지임. HIGH/CRITICAL 0 확인 |
| AMD64 | manifest `sha256:e8ce4eb143fe0c8236b0d144bc42b117f4a4196b814b950bd96cbc3ebed38785` | 실제 imageID와 대조 필요 |
| 권한 | non-root·모든 capability 제거·권한 상승 금지·읽기 전용 rootfs | ServiceAccount token 자동 마운트 없음 |
| 노출 | Pod 네트워크와 ClusterIP Service만 사용 | 외부 ingress·호스트 9100 리스너 추가 없음 |
| 수집 | CPU·meminfo만 활성화 | 기존 node-exporter alerting/추가 recording 활성화 없음 |
| 자원 | 요청 25m/32Mi, 제한 100m/64Mi | filesystem·network collector는 비활성 |
| 운영 추가 | monitoring의 동일 이름 `personal-server-monitoring-prometheus-node-exporter`인 SA·Service·DaemonSet·ServiceMonitor | 정확한 4개 신규 자원만 생성 |
| 보호 | 기존 앱·PVC·백업 일정·인증 Secret·Grafana 대시보드·Caddy·Tunnel 보존 | Prometheus operator의 사전 식별된 scrape-config Secret 자동 갱신만 필요 |

### 적용·원복 절차

1. 최신 main의 검사 통과 소스·변경 경로·이미지 신원을 고정하고 독립 검토함.
2. N100 main의 추적 clean·hook·기존 미추적 파일과 신규 대상 부재를 확인한 뒤 정확한 검증 커밋으로 fast-forward함. 강제 reset·덮어쓰기 없음.
3. 운영 Pod와 동일한 이미지·args·보안·RO 마운트를 사용하는 소유 임시 Pod/SA로 실제 WSL 수집을 시험함. 원본 `/proc`의 CPU·메모리와 대조한 뒤 임시 자원의 UID 소멸을 확인함.
4. 성공한 후보만 SA → Service → DaemonSet → ServiceMonitor 순서로 신규 생성함. 기존 자원의 동명 덮어쓰기를 거부하며 UID·spec fingerprint를 기록함.
5. scrape UP·CPU/meminfo 성공·기존 CPU/메모리 recording·실제 Grafana 패널을 확인함. rate 워밍업은 제한된 시간 안에서 기다림.
6. 실패 시 이번에 생성한 정확한 UID의 ServiceMonitor → DaemonSet/소유 Pod → Service → SA만 UID/RV 조건으로 삭제함. 일반 종료 유예를 사용하고 실제 소멸과 기존 서비스 상태를 확인함. 삭제 응답이 유실돼도 동일 UID의 삭제 진행 상태를 먼저 조회하며 무조건 재삭제하지 않음.
7. Portal/Book 외부 health는 전후 각각 10초 이상 간격 3회 확인함. 수집기는 외부 endpoint를 만들지 않으므로 내부 metrics와 Grafana에서 별도 확인함.

현재 제한 적용은 Helm release revision·manifest를 변경하지 않음. 생성 자원에는 기존 release의 정확한 소유 메타데이터를 유지하고 Git values와 렌더 결과를 대조함. 현재 Helm rollback/uninstall이 이 추가 자원을 정리한다고 가정하지 않으며, 이번 원복은 위 UID 조건부 절차를 사용함. 향후 전체 Helm upgrade는 별도 작업임.

## 검토 결과

| 구분 | 결과 | 비고 |
|---|---|---|
| 로컬 전체 검사 | 9개 묶음 PASS | 이후 최종 이미지·최신 main 영향 범위 131개 재검증 PASS |
| 후보 이미지 | HIGH 0 / CRITICAL 0 | 안정 v1.12.1-distroless는 HIGH 15로 미실행 |
| 이미지 신원 | index·AMD64 manifest·config blob 내용 해시 대조 PASS | 개발 이미지 수용 별도 승인 완료 |
| 독립 검토 | 진행 중 | 구성 구현자와 다른 Sol high가 source·절차·사후 원본 검토 |
| 실제 WSL·운영 | 준비 중 | 미수행 결과를 PASS로 표현하지 않음 |

## 확인 필요 사항

- 실제 WSL canary 수집·원본 일치·정상 종료와 새 수집기의 제한된 부하 확인 필요.
- 운영 scrape·recording·실제 패널·기존 Prometheus 무재시작·보호 대상 유지 확인 필요.
- 개발 이미지의 정식 보안 수정 릴리스 전환은 후속 버전 검토 대상임.

## 후속 조치

현재 승인 범위의 CI·독립 검토·실제 적용을 계속하고 성공 또는 원복 결과를 정확한 커밋·실행 증거와 함께 갱신함. 승인 범위를 다른 exporter·경보·모니터링 전체 업그레이드로 확대하지 않음.
