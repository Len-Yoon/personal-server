# N100 Loki·Ansible 실습 환경 설계

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | N100 Loki·Ansible 실습 환경 설계 |
| 작성일 | 2026-10-01 |
| 목적 | 기존 개인 서버 운영 경계를 보존하면서 로그 관측과 멱등형 자동화의 실습·포트폴리오 증적을 추가함 |
| 대상 | N100 WSL2의 K3s 및 Docker Compose 실습 전용 자원 |
| 상태 | 2026-10-04 기준 Loki 단계별 구현·검토 기록 있음. Ansible 최종 계약 검토·CI 통합 미완료, N100 미적용 |

## 핵심 요약

기존 `monitoring` Helm release와 운영 Compose·K3s 앱을 변경하지 않고, 별도 `observability-lab` namespace에 Loki와 비민감 샘플 앱의 로그 수집기를 배치함. Grafana에는 이 전용 Loki 데이터 소스와 실습 대시보드만 추가함. Ansible은 N100 WSL의 `localhost`만 대상으로 하며, loopback 전용 샘플 Compose 앱의 배치·상태 확인·명시적 롤백만 수행함.

두 실습은 기존 Portal·백업·SRE 자동화·Secret·PVC·Caddy·Tunnel·scheduler와 독립적으로 동작해야 함. 실제 N100 적용은 구현·저장소 검증·독립 검토 완료 후 별도 운영 승인으로만 수행함.

## 범위와 제외 범위

| 구분 | 처리 | 제외 |
|---|---|---|
| Loki | `observability-lab` namespace의 단일 Loki 인스턴스와 전용 저장소·수집기 | 기존 Prometheus·Grafana PVC, Alertmanager, Relay, 운영 서비스 로그 |
| 로그 대상 | 같은 namespace의 샘플 앱 stdout만 allowlist로 수집 | `personal-server`, `monitoring`의 기존 워크로드·백업 Job·시스템 로그 |
| Grafana | Loki 데이터 소스 및 실습 대시보드 ConfigMap 추가 | Grafana 인증·공개 ingress·기존 Prometheus 대시보드 변경 |
| Ansible | WSL `localhost`, 전용 Compose project·디렉터리·loopback 포트 | SSH, Windows 작업, `become`, 운영 Compose·K3s·PVC·Secret·Caddy·Tunnel·bootstrap·scheduler |
| 배포 | 저장소 구현·검증·PR·병합까지 준비 | N100 동기화·이미지 반입·K3s 적용·실제 서비스 재시작은 별도 운영 승인 전 미수행 |

## 설계 대안과 선택

| 대안 | 장점 | 제외 또는 선택 사유 |
|---|---|---|
| 기존 `monitoring` namespace에 Loki 추가 | Grafana와 가까움 | 기존 검증기가 PVC 수를 2개로 고정하고, 기존 설치·제거 도구가 Helm release·전체 PVC에 영향을 줄 수 있어 제외함 |
| 운영 앱 전체 로그 수집 | 즉시 높은 관측 범위 | 요청·인증·메모 관련 정보가 포함될 수 있어 로그 검토 전에는 제외함 |
| 분리된 실습 namespace와 샘플 앱 | 권한·데이터·롤백 경계가 명확함 | 선택함. 이후 비민감 로그 계약을 검토한 서비스만 별도 변경으로 확장 가능함 |

## Loki 실습 구성

### 구성 요소

1. `observability-lab` namespace에 Loki Deployment·ClusterIP Service·전용 PVC를 배치함.
2. 같은 namespace에 비민감 고정 문장만 stdout에 쓰는 샘플 앱 Deployment를 배치함.
3. 수집기는 같은 namespace의 label allowlist 대상 Pod 로그만 Kubernetes API로 읽어 Loki에 전송함.
4. 수집기 ServiceAccount는 `observability-lab` namespace의 `pods` 및 `pods/log`에 필요한 읽기 권한만 가짐. ClusterRole, cluster-admin, Docker socket, hostPath mount를 사용하지 않음.
5. Grafana는 `monitoring` namespace의 데이터 소스 ConfigMap으로 Loki 내부 Service를 읽기 전용 조회함. 공개 경로와 Grafana 사용자 인증 방식은 변경하지 않음.

### 저장·자원·보안 경계

| 항목 | 계약 |
|---|---|
| 로그 보존 | 24h 보존과 local-path PVC 1Gi 요청을 선언함. 비동기 보존 및 PVC 요청은 실제 디스크 사용량 상한을 강제하지 않으므로 호스트 디스크 여유·경보 확인 필요함 |
| 자원 | Loki·수집기·샘플 앱 각각 request와 limit을 선언함. 사전 점검에서 여유가 부족하면 적용하지 않음 |
| 로그 내용 | 고정된 비민감 샘플 문장과 최소 label만 허용함. 토큰, 쿠키, 요청 본문, 사용자 입력, 내부 주소를 넣지 않음 |
| 네트워크 | Loki는 ClusterIP 및 ingress NetworkPolicy를 사용함. 동일 namespace Alloy와 monitoring Grafana의 TCP 3100만 허용함. 샘플 앱은 로그 생성용이며 Service를 만들지 않음. 실제 CNI 정책 집행 확인 필요함 |
| 파일시스템 | Loki 전용 PVC 외 기존 PVC·hostPath·Secret volume을 mount하지 않음 |
| 컨테이너 | non-root, privilege escalation 금지, capability 전체 drop, read-only root filesystem을 기본으로 함. 필요한 임시 쓰기는 `emptyDir`로 한정함 |

### 검증과 롤백

- manifest 정적 계약은 namespace, RBAC, 보존·자원 상한, PVC 소유, 보안 context, allowlist 로그 대상, 외부 노출 부재를 검사함.
- Loki 전용 검증 도구는 Deployment Ready, 전용 PVC Bound, Grafana 내부 데이터 소스, allowlist 샘플 로그 한 건의 LogQL 조회만 확인함.
- 기존 `monitoring-verify.sh`의 Grafana·Prometheus PVC 계약은 유지함. Loki 검증은 별도 도구로 분리함.
- 실패 시 Loki·수집기·샘플 앱·전용 데이터 소스만 제거함. 기존 `monitoring-uninstall.sh`와 기존 Helm release 제거를 사용하지 않음.

## Ansible 실습 구성

### 대상과 동작

inventory는 `localhost ansible_connection=local` 한 호스트만 정의함. playbook은 전용 project 이름, 전용 디렉터리, `127.0.0.1`에만 바인딩하는 샘플 Compose 앱만 관리함.

| 단계 | 동작 | 성공 기준 |
|---|---|---|
| preflight | Ansible·Docker Compose 가용 여부, 전용 포트·전용 디렉터리 충돌 여부 확인 | 운영 서비스·포트·파일을 건드리지 않고 실패 이유를 표시함 |
| deploy | 전용 Compose 정의와 고정 비민감 응답 파일을 배치하고 샘플 앱을 기동함 | loopback health가 HTTP 200을 반환함 |
| verify | 컨테이너 상태와 loopback health를 확인함 | 외부 공개 경로 없이 정상 응답을 확인함 |
| idempotency | 같은 playbook을 재실행함 | 두 번째 실행에서 변경이 0건임 |
| rollback | 명시적 tag 또는 별도 playbook으로 샘플 project만 중지·정리함 | 전용 컨테이너·전용 디렉터리만 제거하고 운영 자원은 보존함 |

Ansible은 `become`을 사용하지 않고, 기존 배포·백업·복구 도구를 호출하거나 재구현하지 않음. 인벤토리·변수·템플릿·로그에 비밀번호, 토큰, 실제 계정명, 내부 주소를 기록하지 않음.

## 운영 적용 전후 기준

### 사전 점검

1. N100 저장소가 검증된 `main` 커밋으로 fast-forward 가능한지 확인함.
2. K3s node, 기존 monitoring Pod·PVC, Prometheus scrape, Grafana, Alertmanager→Relay가 정상인지 읽기 전용으로 확인함.
3. 실제 가용 메모리·디스크와 Loki 요청·제한·PVC 요청량을 대조함. local-path의 디스크 강제 상한은 보장되지 않으므로 디스크 보호 방안을 확인하고, 여유가 부족하면 적용하지 않음.
4. 외부 Portal health를 10초 간격 3회 확인함.
5. 수집 대상 샘플 로그에 민감 정보가 없음을 값 자체를 출력하지 않는 방식으로 확인함.

### 사후 점검

1. Loki·수집기·샘플 앱 Ready와 Loki PVC Bound를 확인함.
2. Grafana에서 allowlist 샘플 로그만 조회되는지 확인함.
3. 기존 monitoring 검증, Prometheus scrape, Relay, SLO 경로가 유지되는지 확인함.
4. Portal과 변경 대상의 외부 health를 각각 10초 간격 3회 확인함.
5. 결과는 저장소 병합, N100 동기화, 이미지 반입, 수동 검증, 자동 실행 활성화, 외부 health로 분리 기록함.

## 확인 필요 사항

- N100의 실제 가용 메모리·디스크, 현재 로그 형식, Ansible·Docker Compose 설치 상태는 운영 사전 점검에서 확인 필요함.
- 운영 서비스 로그 수집 확대는 로그 민감정보 검토와 별도 승인 없이는 수행하지 않음.
- Grafana data source sidecar의 현재 label·갱신 계약은 구현 검증에서 렌더링·테스트로 확인 필요함.

## 후속 조치

1. 이 설계를 기준으로 Loki 실습과 Ansible 실습을 각각 독립 구현 계획으로 분리함.
2. 계획 검토·승인 후 테스트 우선으로 저장소 구현을 수행함.
3. PR CI·독립 검토가 통과한 정확한 커밋만 N100 사전 점검과 별도 운영 승인 대상으로 제시함.
