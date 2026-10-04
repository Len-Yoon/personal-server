# Personal Server DevOps 포트폴리오

## 문서 정보

| 항목 | 내용 |
|---|---|
| 작성 기준일 | 2026-10-04 |
| 목적 | 면접에서 운영 경험·설계 판단·검증 근거를 설명하는 자료 |
| 기준 자료 | 저장소 README, 운영 문서, Loki·Ansible 코드 및 작업별 검증 기록 |
| 개발 기준 | [PR #331](https://github.com/Len-Yoon/personal-server/pull/331), 병합 커밋 `f66a42a` |
| 비고 | 저장소·운영 검증 결과와 미검증 항목을 구분함. [운영 검증 결과](reviews/20261004_Loki_Ansible_실습_N100운영검증결과.md) 참조 |

## 핵심 요약

Windows N100 한 대의 WSL2에서 K3s와 Docker Compose를 함께 사용하는 개인 서버 프로젝트임. 공개 서비스 경로, 서비스별 실행 주체, 백업·복원 검증, 관측·알림, 변경 검증을 관리함. 단일 호스트의 자원 제약 안에서 중복 쓰기와 복구 실패를 줄이는 운영 절차를 구성한 경험을 설명할 수 있음.

Loki·Alloy 로그 관측과 Ansible localhost 자동화는 구현·CI·Trivy·독립 검토 후 병합하고 승인된 N100 실습 적용을 완료함. 실제 샘플 로그 조회, Ansible 2회차 변경 0건·롤백·재배치와 기존 관측 자원·Portal health 보존을 확인함. Grafana 화면/API 조회, CNI 원인 검증, retention·hard quota는 미검증으로 구분함.

## 상세 내용

### 1. 운영 구조와 판단

| 주제 | 구현·운영 기준 | 선택 이유 | 근거·비고 |
|---|---|---|---|
| 서비스 공개 | Cloudflare Tunnel → Caddy → 서비스 경로이며 차량 callback은 예외 경로임 | 도메인 라우팅과 내부 실행 환경을 연결함 | [운영 구조](../README.md#구조와-기술-선택), [Tunnel 경계](cloudflare-tunnel.md). 예외 경로를 포함해 설명 필요 |
| 실행 주체 | K3s 앱과 Compose 서비스를 함께 운영하며 데이터 writer를 하나로 제한함 | SQLite·파일 기반 저장에서 중복 writer의 데이터 충돌을 방지함 | [현재 운영 기준](operations-reference.md) |
| 변경 전달 | 기능 브랜치·PR 검증 뒤 병합하고, 배포 분류 및 운영 적용을 별도 확인함 | 코드 검증과 실제 서버 적용의 증적을 구분함 | [배포 흐름](../README.md#배포-흐름) |
| 백업·복구 | 암호화 원격 백업과 격리 복원 검증을 수행함 | 백업 파일 존재만으로 복구 가능성을 판단하지 않음 | [백업 적용 기록](reviews/20260928_서비스별_PVC_백업_자동실행_적용결과.md), [K3s 운영](../infra/k8s/README.md) |
| 관측·알림 | Prometheus·Grafana·Alertmanager·Telegram relay와 외부 health 감시를 사용함 | 내부 상태와 외부 접근 경로를 함께 확인함 | [운영 로드맵](operations-roadmap.md), [공개 감시](public-uptime-monitor.md) |
| 복구 훈련 | 격리된 Pod 복구 훈련 및 월간 SRE 통합 점검을 구성함 | 운영 서비스를 중단하지 않고 복구 경로와 증적을 확인함 | [복구 훈련](recovery-drill.md). 실제 호스트 장애 전체를 재현한 것은 아님 |

### 2. 후속 실습의 개발 상태

| 항목 | 설계·구현 | 확인된 검증 | 남은 조건 |
|---|---|---|---|
| Loki·Alloy | 전용 namespace, 샘플 로그 allowlist, namespace 제한 RBAC, 비root 실행, NetworkPolicy 구현됨 | Deployment 3개 `1/1`·PVC Bound, 실제 샘플 로그 조회 통과 | 최초 기동 직후 조회 실패 후 무변경 재조회 성공. 제한된 재시도 개선, CNI 원인·retention·hard quota 확인 필요 |
| Grafana 연동 | Loki datasource·샘플 dashboard ConfigMap과 datasource sidecar 값 추가됨 | UID `loki-lab` 등록 로그·파일 2개 및 Grafana→Loki ready 확인 | 대시보드 화면/API의 실제 로그 표시 조회 미수행 |
| Ansible | localhost·loopback 전용 Compose 배치·health·소유 범위 검증 롤백 구현됨 | 소유권 조건 우회 변이 20/20 검출. syntax-check 2개·check mode·최초 배치 changed=5·2회차 changed=0·rollback changed=2·재배치 changed=5, 모두 failed=0 | 최종 재배치 후 HTTP 정상. 운영 앱 전체 자동화로 확대하지 않음 |
| CI 통합·병합 | Loki 계약은 K8s 그룹, Ansible 계약은 maintenance 그룹에 1회 등록됨 | maintenance 546건, 9개 그룹·98개 테스트 파일 배정 검증, PR·main CI·Trivy·독립 검토 및 `f66a42a` 병합됨 | 안전 자동배포는 `blocked_path`로 제외됨 |
| 운영 적용 | 동일 커밋 동기화·승인된 수동 실습 적용 완료 | 샘플 로그 조회·Ansible 리허설 통과, 독립 사후 검토 APPROVE. Portal 전후 10초 간격 각 3회 정상, monitoring 4 Deployment·2 StatefulSet 준비 상태 유지 | Loki 실제 rollback·Grafana 화면/API·CNI 원인·retention·hard quota 미검증 |

검사 건수는 서로 다른 단계의 실행 기록임. 합산한 고유 테스트 수로 사용하지 않음. 실행 증적과 미검증 범위는 [운영 검증 결과](reviews/20261004_Loki_Ansible_실습_N100운영검증결과.md)를 따름.

### 3. 면접 설명 예시

“단일 N100의 WSL2에서 개인 서비스를 운영하며, K3s와 Compose의 실행 주체를 구분하고 SQLite·파일 기반 서비스가 동시에 데이터를 쓰지 않도록 관리했습니다. 백업은 원격 암호화 저장에 더해 격리 복원까지 검증했고, 내부 지표와 외부 health 감시를 함께 사용했습니다. 변경은 PR 검사와 실제 서버 적용 기록을 구분해 추적했습니다. 별도 namespace의 Loki 실습에서는 실제 샘플 로그를 조회했고, localhost Ansible 실습에서는 2회차 변경 0건과 롤백·재배치를 확인했습니다. 기존 운영 서비스는 보존했으며, 실습 검증 결과와 아직 확인하지 않은 항목을 분리해 기록했습니다.”

### 4. 예상 질문과 답변 근거

| 질문 | 설명할 핵심 | 한계·비고 |
|---|---|---|
| 왜 K3s를 선택했는가? | 로컬 자원에서 Kubernetes의 Deployment·Service·PVC·probe·RBAC를 적용하고 운영 절차를 경험함 | 다중 노드 HA나 대규모 클러스터 운영 경험으로 표현하지 않음 |
| 장애가 나면 어떻게 확인하는가? | 외부 health, 내부 지표·알림, 실행 주체와 증적을 대조함 | health 하나의 성공이 모든 기능·데이터의 정상성을 보장하지 않음 |
| 백업 성공을 어떻게 판단하는가? | 암호화 백업과 격리 복원 결과를 함께 확인함 | 모든 실제 재해 상황과 RTO·RPO를 실측했다고 주장하지 않음 |
| 왜 Loki를 따로 구성했는가? | 기존 monitoring PVC·제거 도구와의 충돌을 피하고 샘플 로그·권한·삭제 범위를 분리함 | 샘플 실습이며 운영 앱 전체 로그를 수집하지 않음 |
| Ansible에서 멱등성을 증명했는가? | 같은 playbook의 실제 2회차 `changed=0`·`failed=0`을 확인했고 rollback·재배치도 통과함 | 고정 localhost 샘플 앱에 한정된 검증이며 운영 앱 전체의 멱등성을 증명한 것은 아님 |
| 설계의 제약은 무엇인가? | 단일 호스트 장애, WSL2 관측 제약, 단일 writer, 미완료 SLI·에러 버짓 산정을 설명함 | [현재 한계](../README.md#현재-한계와-다음-작업) 참조 |

## 검토 결과

- 운영 사실은 기존 운영 문서·적용 기록을 기준으로 정리함.
- 후속 실습의 구현, 단계별 검사, 독립 검토, 실제 운영 적용을 구분함.
- 응답 시간 개선율·가용성 비율·RTO·RPO·토큰 절감률 등 미측정 성과 수치는 기재하지 않음.

## 확인 필요 사항

- Loki 실제 rollback·Grafana 화면/API 조회·CNI 규칙 원인·24시간 retention·디스크 경보 확인 필요함. local-path PVC 1Gi 요청은 디스크 사용량의 강제 상한이 아님.
- 면접 제출 시 PR·병합 커밋과 최신 운영 증적을 다시 대조해야 함.

## 후속 조치

1. Loki 기동 직후 조회 실패에 대해 검증 도구의 제한된 재시도 개선을 별도 작업으로 수행함.
2. 미검증인 Grafana 화면/API·retention·디스크 경보와 Loki 실제 rollback은 별도 검증 범위에서 확인함.
3. 면접에서는 문제 → 선택 이유 → 검증 근거 → 한계 순서로 설명함.
