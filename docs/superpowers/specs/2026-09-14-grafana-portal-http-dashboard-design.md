# Portal HTTP Grafana 대시보드 설계

## 문서 정보

| 항목 | 내용 |
|---|---|
| 목적 | Portal HTTP Prometheus 메트릭을 Grafana에서 운영 지표로 확인함 |
| 적용 범위 | monitoring namespace의 Grafana dashboard ConfigMap과 명시적 적용 도구 |
| 제외 범위 | Secret, PVC, Caddy, Cloudflare Tunnel, Portal runtime 설정 |

## 설계 결정

| 구분 | 결정 | 근거 |
|---|---|---|
| 배포 단위 | `grafana_dashboard=1` ConfigMap 1개 | kube-prometheus-stack Grafana sidecar 계약을 사용함 |
| 패널 | 최근 5분 요청 수, 상태 코드별 요청 수, 5xx 오류 비율, p95 응답 시간 | 현재 `portal_http_requests_total`과 histogram 메트릭으로 계산 가능함 |
| 적용 | `monitoring-dashboard-apply.sh --apply`에서 해당 ConfigMap만 apply함 | 최초 monitoring 설치 도구와 운영 중 대시보드 추가를 분리함 |
| 검증 | 도구는 Grafana Deployment와 ConfigMap 라벨을 확인하고, 운영자는 Prometheus datasource·메트릭을 수동 확인함 | Secret 값을 읽거나 출력하지 않음 |
| 롤백 | `--rollback`에서 해당 ConfigMap만 삭제함 | 모니터링 스택·Portal runtime에는 영향을 주지 않음 |

## 처리 흐름

인자 없는 실행은 usage 오류로 종료함. `--check`은 Grafana Deployment와 ConfigMap의 dashboard label을 읽기 전용으로 확인함. `--apply`는 동일 사전 검증과 server dry-run 뒤 dashboard ConfigMap을 client-side apply함. Grafana sidecar가 label을 감지하여 대시보드를 로드함. `--rollback`은 지정된 ConfigMap만 삭제함.

## 확인 필요 사항

- Grafana UI에서 datasource가 `Prometheus`로 표시되는지는 N100 적용 직후 포트포워드 화면에서 확인 필요함.
- 적용은 N100 운영 변경이므로 저장소 검증과 별도로 운영자 실행 결과를 확인 필요함.
