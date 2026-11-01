# Grafana WSL CPU·메모리 수집 검증

## 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 기준 시각 | 2026-11-01 22:33 | Asia/Seoul, 실제 화면 재촬영 기준 |
| 작성·실행 | 주 담당 Codex | 구성·절차 구현자와 최종 독립 검토자 분리 |
| 기준 | 기존 kube-prometheus-stack 88.6.1의 scrape·node.rules | 별도 Git 소유 최소 exporter, 기존 release 전체 upgrade 제외 |
| 목적 | 단일 Cluster의 CPU·메모리 사용률 결측 복구 | Windows 물리 호스트가 아닌 WSL2 노드 범위 |
| 승인 | 실제 WSL 검증·운영 적용·문제 시 원복과 결과 확인 | 이번 수집기 추가에 한정 |
| 추가 승인 | 고정된 공식 개발 이미지로 시험 후 적용 | 안정 이미지의 신규 HIGH 15건 발견 후 별도 수용 |

## 핵심 요약

실제 WSL 시험과 N100 운영 적용을 완료함. CPU 4개·MemTotal 8,194,215,936바이트가 WSL 원본과 일치하며, 기존 CPU·메모리 recording과 실제 Grafana 패널의 결측이 해소됨. 호스트 볼륨 없이 CPU·meminfo만 수집하고 hostNetwork·hostPID·hostIPC를 사용하지 않음. 기존 16개 UP target·Prometheus Pod UID와 재시작 수·보호 대상을 유지함. 최종 운영 적용은 성공하여 운영 원복은 불필요했으며 초기 시험의 소유 임시 자원은 모두 정리함.

## 상세 내용

| 대상 | 설정·동작 | 비고 |
|---|---|---|
| 이미지 | 공식 `master-distroless`의 고정 index `sha256:2aeca551ca3cb8c90f11fee9bcdf9859725c48f272d478aa79b9bbc48eda13ed` | 정식 릴리스 전 이미지임. HIGH/CRITICAL 0 확인 |
| AMD64 | manifest `sha256:e8ce4eb143fe0c8236b0d144bc42b117f4a4196b814b950bd96cbc3ebed38785` | 실제 runtime imageID는 위 고정 index digest와 일치 |
| 권한 | non-root·모든 capability 제거·권한 상승 금지·읽기 전용 rootfs | ServiceAccount token 자동 마운트 없음 |
| 노출 | Pod 네트워크와 ClusterIP Service만 사용 | 외부 ingress·호스트 9100 리스너 추가 없음 |
| 수집 | CPU·meminfo만 활성화 | 기존 node-exporter alerting/추가 recording 활성화 없음 |
| 자원 | 요청 25m/32Mi, 제한 100m/64Mi | filesystem·network collector는 비활성 |
| 운영 추가 | monitoring의 동일 이름 `personal-server-monitoring-prometheus-node-exporter`인 SA·Service·DaemonSet·ServiceMonitor | 정확한 4개 신규 자원만 생성 |
| 보호 | 호스트 볼륨 0개, 기존 앱·PVC·백업 일정·인증 Secret·Grafana 대시보드·Caddy·Tunnel 보존 | Prometheus operator의 사전 식별된 scrape-config Secret 자동 갱신만 필요 |

### 적용·원복 절차

1. 최신 main의 검사 통과 소스·변경 경로·이미지 신원을 고정하고 독립 검토함.
2. N100 main의 추적 clean·hook·기존 미추적 파일과 신규 대상 부재를 확인한 뒤 정확한 검증 커밋으로 fast-forward함. 강제 reset·덮어쓰기 없음.
3. 운영 Pod와 동일한 이미지·args·보안·마운트 없는 설정을 사용하는 소유 임시 Pod/SA로 실제 WSL 수집을 시험함. 원본 `/proc`의 CPU·메모리와 대조한 뒤 임시 자원의 UID 소멸을 확인함. 컨테이너 한도에 가상화된 값이면 운영 적용하지 않음.
4. 성공한 후보만 SA → Service → DaemonSet → ServiceMonitor 순서로 신규 생성함. 기존 자원의 동명 덮어쓰기를 거부하며 UID·spec fingerprint를 기록함.
5. scrape UP·CPU/meminfo 성공·기존 CPU/메모리 recording·실제 Grafana 패널을 확인함. rate 워밍업은 제한된 시간 안에서 기다림.
6. 실패 시 이번에 생성한 정확한 UID의 ServiceMonitor → DaemonSet/소유 Pod → Service → SA만 UID/RV 조건으로 삭제함. 일반 종료 유예를 사용하고 실제 소멸과 기존 서비스 상태를 확인함. 삭제 응답이 유실돼도 동일 UID의 삭제 진행 상태를 먼저 조회하며 무조건 재삭제하지 않음.
7. Portal/Book 외부 health는 전후 각각 10초 이상 간격 3회 확인함. 수집기는 외부 endpoint를 만들지 않으므로 내부 metrics와 Grafana에서 별도 확인함.

현재 제한 적용은 Helm release revision·manifest를 변경하지 않음. 표준 Helm node-exporter는 계속 비활성화하고 신규 4개 자원은 Git 소유로 분리하며 Helm 소유 annotation을 붙이지 않음. 기존 Helm rollback/uninstall이 이 자원을 정리한다고 가정하지 않으며 이번 원복은 위 UID 조건부 절차를 사용함. 향후 Helm exporter를 켜면 동명 Git 자원과 소유권 충돌이 발생할 수 있으므로 별도 이관 검토가 필요함.

## 검토 결과

| 구분 | 결과 | 비고 |
|---|---|---|
| 로컬 전체 검사 | 9개 묶음 PASS | 최종 호스트 마운트 제거 후 관련 구성·문서 41개 PASS, 영향 없는 전체 결과 재사용 |
| 후보 이미지 | HIGH 0 / CRITICAL 0 | 안정 v1.12.1-distroless는 HIGH 15로 미실행 |
| 이미지 신원 | index·AMD64 manifest·config blob 내용 해시 대조 PASS | 개발 이미지 수용 별도 승인 완료 |
| 설정 보안 | 최초 hostPath 안은 CI KSV-0121 HIGH로 미병합·미실행 | 예외 처리 없이 마운트 0개로 축소, 최종 PR/main Trivy PASS |
| 저장소 병합 | PR #466, main `514cc30b76332a21f21e85b1dc49d05492401533` | PR 필수 15개 검사·main CI 성공. Deploy N100은 `blocked_path`로 실제 배포 제외 |
| N100 동기화 | 정확한 위 커밋으로 fast-forward PASS | main 추적 clean·hook 부재·기존 미추적 파일 2개 보존 |
| 이미지 반입·실행 | 실제 runtime index digest 일치 PASS | 새 exporter만 해당 이미지 사용, 기존 앱 이미지 교체 없음 |
| 수동 검증 | WSL 원본·운영 수집·recording·실제 UI PASS | 임시 Pod/SA 정리 후 동일 설정의 4개 자원 신규 생성 |
| 자동 실행 활성화 | 별도 scheduler·복구 자동화 변경 해당 없음 | 신규 DaemonSet의 정상 유지와 별도 운영 자동화를 구분함 |
| 외부 health | Portal·Book 전후 각각 3회 HTTP 200 | 완료 시각 기준 간격 10초 이상인 사전·사후 원본 보존 |
| 최종 독립 검토 | 구현에 참여하지 않은 Sol high의 소스·실제 원격 GET PASS | 자기 검증·CI·독립 검토를 별도 증거로 구분함 |

### 실제 수집·화면 근거

| 항목 | 실제 결과 | 비고 |
|---|---|---|
| WSL CPU | 4개, USER_HZ 환산 원본 대조·카운터 증가 PASS | 컨테이너 CPU 제한 100m를 노드 용량으로 오인하지 않음 |
| WSL 메모리 | 8,194,215,936바이트 원본 일치 | 컨테이너 메모리 제한 64Mi와 구분함 |
| collector | CPU·meminfo 성공 1, 다른 node collector 비활성 | 호스트 마운트·자동 토큰 마운트 없음 |
| 실행 신원 | version `1.12.1`, revision `f37c4cdea9a60fe0cc9c348a8af6ef501ef0c187`, Go `go1.27.3` | 버전 문자열과 별개로 공식 개발 이미지 digest를 고정함 |
| Prometheus | 신규 node-exporter 1 UP, 기존 16 UP 유지 | 기존 CPU·메모리 recording 2개 정상, 추가 collector 불필요 |
| 적용 직후 recording | CPU 약 3.45%, 메모리 약 38.68% | rate 계산 초기 대기 후 2회 검증, 지속 수치로 확대하지 않음 |
| 실제 UI | CPU 18.3%, 메모리 38.9% | 2026-11-01 22:33, 정확한 단일 Cluster UID·실제 datasource 질의 10회 |
| 유지·정리 | 수집기 1 Ready·재시작 0, 임시 Pod·SA 각각 0 | 기존 Prometheus UID·재시작 수와 보호 baseline 일치 |

단일 Cluster UID는 `efa86fd1d0c121a26444b636a3f509a8`임. [실제 촬영 화면](images/grafana-k3s-overview.png)은 기존 1440 × 1000 프레임·배치를 유지하고 수치·그래프·표를 브라우저 원본 그대로 사용함. Windows 물리 호스트 지표로 해석하지 않음.

### 시험 중 발견·보완과 원복 구분

| 발견 사항 | 실제 영향·조치 | 검증 |
|---|---|---|
| 동기화 작업 ID 형식 | canonical UUID 검사에서 생성 전 중단함 | 원격 변경 부재 확인 후 형식 보완·정상 동기화 |
| client dry-run JSON 출력 | 여러 YAML 문서가 연속 JSON으로 출력되어 생성 전 중단함 | 실제 자원 부재 확인·엄격한 4개 객체 stream 파서 보완·독립 재검토 |
| Kubernetes 기본값·스케줄링 | probe의 0 기본값 생략으로 시험 신원 검사 실패, scheduler의 nodeName 추가로 최초 정리 검사 중단함 | 실제 이미지·UID·원본 차이 확인 후 해당 두 경계만 보완, UID/RV 조건으로 임시 Pod/SA 정리·실제 부재 확인 |
| journal 직렬화 | 독립 사후 검토에서 tuple/list 표현 차이의 거짓 실패 가능성을 실행 전 발견함 | canonical JSON 동등 비교만 보완, UID·재시작·순서·인증 Secret 변경 거부 유지, 별도 post 실제 PASS |
| 최종 운영 | CPU·메모리·기존 서비스 검증 성공 | 운영 4개 자원 원복은 미수행·불필요. UID 조건 원복의 fixture와 실제 시험 자원 정리를 운영 원복 실행으로 표현하지 않음 |

최종 임시 실행 도구는 SHA-256 `299e0fa94c59c1cb050a071123e7100210222ef935b30b732843ed0040adb306`이며 fixture 65개 PASS와 별도 독립 재검토를 완료함. 실제 적용은 직전 도구 `0a84dce9e0d277c08e9c91bc795e0dbf88fd64a09f55abeb8aedcfa535132229`, 별도 post는 최종 도구로 수행함. 그 사이 운영 설정·이미지·수집 범위 변경은 없음.

최초 사전 health는 2026-11-01 21:57, 사후 health는 22:27에 양 서비스 각각 3회·완료 간격 10초 이상·200을 확인함. 22:20의 추가 직전 점검도 6회 모두 200이지만 마지막 완료 간격은 약 9.2~9.7초여서 엄격한 10초 증거와 구분함. 사후 도구는 두 응답 완료 후 10.2초 대기하도록 보완함.

### 역할·증거 보존

| 담당 | 모델·추론·범위 | 결과·선택 이유 |
|---|---|---|
| 주 담당 | Codex, 통합·실제 실행·촬영·문서 | 자기 검증이며 최종 독립 검토와 구분함 |
| 구성 구현 | Sol medium, YAML·관련 회귀 | 범위가 명확한 구성 구현 담당 |
| 절차 구현 | Sol high, 제한 적용·UID 원복·fixture | 운영 실패 비용을 고려한 전문 구현 담당 |
| 최종 독립 검토 | 별도 Sol high, 구현 파일 작성 없음 | source·금지 영역·보완 diff·실제 N100 GET 사전/사후 직접 대조 PASS |

소스 독립성 하네스는 base `1311bf1` → head `44982b8` 대상 PASS임. main의 squash 커밋은 `514cc30`이며 동일 트리임. 운영 journal은 작업 UUID `83af8222-4696-438e-9551-8e7ae248c901`로 소유 UID를 구분함. 비밀값·내부 주소·계정은 증거에 저장하지 않으며, 소스 검토 receipt·검사·공개 이미지 신원·운영/화면/health·독립 GET 결과는 로컬 `build/grafana-wsl-node-metrics/514cc30/`에 보존함.

## 확인 필요 사항

- 개발 이미지는 정식 릴리스 전 버전임. 정식 보안 수정 릴리스로의 전환은 별도 후속 검토 필요.
- 이번 결과는 WSL2 노드의 CPU·메모리 범위임. Windows 물리 호스트·네트워크·디스크 수집과 장기간 부하·가용성은 미검증임.

## 후속 조치

이번 운영 승인은 검증·적용·정리·사후 확인 완료로 소진됨. 검사 통과 main의 정확한 소스를 먼저 운영 적용해야 했으므로 적용 후에 확정된 문서·이미지 결과는 별도 PR로 반영함. 이 문서 반영을 새 운영 배포로 해석하지 않으며, 다른 exporter·경보·전체 upgrade·새 운영 재실행으로 승인을 확대하지 않음.
