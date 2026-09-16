# K3s 애플리케이션 전환 설계서

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | K3s 애플리케이션 전환 설계서 |
| 작성일 | 2026-09-16 |
| 기준 자료 | `docker-compose.n100.yml`, `docker-compose.portal-bridge.yml`, 현재 K3s 운영 문서, 맥북 개발 환경 점검 결과 |
| 목적 | Compose에서 운영 중인 사용자 애플리케이션을 서비스별로 K3s 관리 대상으로 전환하는 기준을 정의함 |
| 비고 | 실제 N100 적용·공개 경로 변경·데이터 복사는 서비스별 사용자 승인 후에만 수행함 |

## 2. 핵심 요약

맥북은 코드 검증과 Linux AMD64 이미지 생성을 담당하고, N100은 검증된 이미지를 K3s에 반입해 실제 운영하는 구조로 분리함. Compose와 K3s가 같은 영속 데이터를 동시에 쓰지 않으며, 한 서비스씩 내부 검증·데이터 전환·공개 경로 전환·롤백 가능 상태를 확인한 후 다음 서비스로 진행함.

전환 대상은 `book-memo`, `youtube-memo`, `crawler-worker`, `car-care-worker` 순서로 정함. `homeops-executor`, `system-agent`, Docker socket proxy, Caddy, Cloudflare Tunnel, Portal 및 Portal PVC는 본 설계의 전환 대상에서 제외함.

## 3. 목표와 범위

### 3.1 목표

| 구분 | 목표 | 완료 기준 |
|---|---|---|
| 실행 관리 | 대상 애플리케이션을 K3s Deployment·Service로 관리함 | Pod Ready, Service 연결, 재기동 동작을 서비스별로 확인함 |
| 빌드 분리 | 맥북에서 Linux AMD64 이미지를 재현 가능하게 생성함 | N100 K3s가 해당 이미지를 정상 실행함 |
| 데이터 보호 | 기존 Compose 데이터의 유실·동시 쓰기를 방지함 | 전환 전·후 데이터 검증과 롤백 기준을 기록함 |
| 외부 연속성 | 기존 공개 URL·Telegram 연동을 유지함 | 공개 health와 기능별 핵심 동작을 확인함 |
| 되돌림 | 서비스별로 기존 Compose 실행 경로를 즉시 복구 가능하게 유지함 | 전환 안정화 전 Compose 정의와 이전 이미지를 보존함 |

### 3.2 대상과 제외 범위

| 구분 | 대상 | 처리 방향 |
|---|---|---|
| 1차 | `book-memo` | 데이터 전환 절차와 K3s Service 기본 패턴을 검증함 |
| 2차 | `youtube-memo` | 1차와 같은 패턴으로 데이터·Telegram 연동을 검증함 |
| 3차 | `crawler-worker` | 수집 상태 파일, 인증된 metrics, `NewsCollectionStale` 경고 경계를 보존함 |
| 4차 | `car-care-worker` | OAuth callback과 Telegram 연동을 포함한 별도 사전 점검 후 전환함 |
| 제외 | Portal·Portal PVC·Secret·운영 데이터 원본 | 수정·삭제·재생성하지 않음 |
| 제외 | Caddy·Cloudflare Tunnel·Windows/WSL 부팅 복구 | 앱 전환과 분리하여 후속 설계로 관리함 |
| 제외 | `homeops-executor`, `system-agent`, Docker socket proxy | Docker 의존 경계이므로 별도 설계 전까지 Compose 유지함 |

## 4. 전환 아키텍처

### 4.1 빌드·반입 경로

```text
맥북 소스·테스트
  → Linux AMD64 OCI 이미지 생성
  → Tailscale SSH를 통한 N100 반입
  → K3s 컨테이너 런타임 이미지 import
  → K3s Deployment·Service 적용
```

맥북은 ARM 환경이므로 이미지 생성 시 대상 플랫폼을 Linux AMD64로 고정함. 새 외부 컨테이너 레지스트리, 새 이미지 pull Secret, 새 자격 증명은 추가하지 않음. 이미지 반입·매니페스트 적용은 서비스별 검증과 사용자 승인 후에만 수행함.

### 4.2 서비스 전환 상태

| 단계 | Compose | K3s | 데이터 쓰기 | 공개 요청 | 다음 단계 조건 |
|---|---|---|---|---|---|
| 준비 | 실행 | 미적용 | Compose만 허용 | Compose | 이미지·매니페스트 정적 검증 성공 |
| 내부 검증 | 실행 | 실행 | Compose만 허용 | Compose | K3s Pod·Service·내부 health 정상 |
| 데이터 전환 | 일시 중지 | 실행 대기 | 양쪽 쓰기 중지 | 유지보수 범위에서 일시 중지 가능 | 복사·무결성 검증 성공 |
| 공개 전환 | 중지 상태 보존 | 실행 | K3s만 허용 | K3s | 외부 health·핵심 기능·알림 확인 |
| 안정화 | 중지 상태 보존 | 실행 | K3s만 허용 | K3s | 서비스별 안정화 기준 충족 |
| 정리 | 제거 가능 | 실행 | K3s만 허용 | K3s | 사용자 승인 후 Compose 정의 제거 |

Compose와 K3s는 동일 데이터 디렉터리 또는 동일 데이터베이스에 동시에 쓰지 않음. 공개 요청의 실제 전환 방법은 현재 Tunnel·Caddy·host port 연결을 읽기 전용으로 대조한 뒤 서비스별 계획에서 확정함. 첫 전환에서 Caddy·Cloudflare Tunnel 설정 자체는 변경하지 않음.

## 5. 서비스별 사전 점검과 완료 조건

| 서비스 | 사전 점검 | K3s 완료 조건 | 롤백 기준 |
|---|---|---|---|
| Book Memo | 데이터 경로·쓰기 동작·공개 health 확인 필요 | 목록·작성·조회와 공개 health 정상 | 공개 요청을 Compose로 복귀하고 K3s만 중지함 |
| YouTube Memo | 데이터 경로·외부 API·Telegram 연동 확인 필요 | 조회·저장·Telegram 핵심 동작 정상 | 공개 요청을 Compose로 복귀하고 K3s만 중지함 |
| crawler-worker | 상태 파일·scheduler·metrics Secret 참조 확인 필요 | 수집 상태·`/health`·인증 metrics·경고 수집 정상 | Compose로 복귀하고 ServiceMonitor·경고 상태를 재확인함 |
| car-care-worker | 데이터 경로·OAuth callback·Telegram 연동 확인 필요 | OAuth callback·health·Telegram 핵심 동작 정상 | callback과 공개 요청을 Compose로 복귀함 |

## 6. 데이터 전환 원칙

1. 서비스별 데이터 원본과 K3s 저장소 유형을 사전 점검으로 확인함. 현재 저장소 구성만으로 StorageClass·PVC 유형을 확정하지 않음.
2. 기존 데이터는 전환 전 읽기 전용 점검으로 수량·형식·핵심 레코드 확인 기준을 정의함.
3. 데이터 복사 시 Compose 쓰기를 중지하고, 복사 완료 후 K3s에서만 쓰기를 허용함.
4. 원본 데이터는 안정화와 사용자 승인 전까지 삭제·덮어쓰기하지 않음.
5. 복사 실패·무결성 불일치·기능 실패 시 공개 경로를 기존 Compose로 복귀하고 원인을 기록함.

## 7. 검증과 보안 경계

| 단계 | 검증 | 성공 기준 |
|---|---|---|
| 맥북 | 단위·계약 테스트, Dockerfile 정적 검사, Linux AMD64 이미지 생성 | 관련 테스트와 이미지 빌드 성공 |
| N100 반입 | 이미지 존재·K3s import 결과 확인 | 이미지 이름·digest 대조 성공 |
| K3s 내부 | Deployment Ready, Service endpoint, 내부 health | Pod 재시작 없이 Ready 및 정상 응답 |
| 공개 전환 | 기존 공개 URL health, 기능별 핵심 요청 | 서비스별 공개 health 정상 |
| 알림 | Telegram 연동 대상 서비스의 장애·복구 또는 정상 전송 기준 | 기존 비밀값 없이 경로 정상 확인 |
| 롤백 | Compose 재개·K3s 중지 후 공개 health | 기존 경로 정상 응답 |

- Secret·토큰·OAuth 값·Telegram 값·개인 데이터는 Git, 이미지, 로그, 문서에 기록하지 않음.
- N100 적용 전후 실제 결과가 불완전하면 같은 변경을 즉시 재실행하지 않고, 이미지·Deployment·Service 상태를 읽기 전용으로 먼저 확인함.
- 서비스별 실제 적용은 사용자 승인 후에만 수행하며, 한 서비스의 전환이 완료되기 전 다음 서비스 전환을 시작하지 않음.

## 8. 단계별 실행 순서

1. 공통 기반: 맥북 AMD64 이미지 생성·N100 반입·K3s 매니페스트 검증 도구를 준비함.
2. Book Memo: 저장소 유형·공개 경로를 사전 점검하고, 내부 배치와 데이터 전환·롤백을 검증함.
3. YouTube Memo: 1차 패턴을 재사용하되 외부 API·Telegram 연동을 별도 검증함.
4. crawler-worker: metrics와 `NewsCollectionStale` 관측성을 유지한 상태로 전환함.
5. car-care-worker: OAuth callback 경계를 확인한 뒤 별도 승인으로 전환함.
6. 운영 보조 서비스·외부 연결 경계: 앞선 네 서비스의 안정화 결과를 기준으로 별도 설계를 진행함.

## 9. 검토 결과

- 맥북의 컨테이너 엔진, Linux AMD64 이미지 생성 도구, Kubernetes CLI, Tailscale SSH 연결을 확인함.
- N100의 실제 StorageClass, 서비스별 데이터 크기, 공개 경로의 세부 upstream, OAuth callback 제약은 아직 읽기 전용 사전 점검이 필요함.
- 새 외부 레지스트리와 새 자격 증명을 도입하지 않는 반입 경로를 우선 적용함.

## 10. 확인 필요 사항

| 항목 | 확인 내용 | 영향 |
|---|---|---|
| K3s 저장소 | N100 StorageClass·PVC 접근 방식·용량 | 데이터 전환 방식 확정 필요 |
| 서비스별 데이터 | 실제 경로·크기·쓰기 중지 방법 | 전환 시간·무결성 검증 기준 필요 |
| 공개 경로 | Tunnel·Caddy·host port의 서비스별 실제 연결 | 무중단 또는 짧은 전환 가능 여부 확인 필요 |
| OAuth | car-care callback 허용 URL·전환 제약 | 4차 전환 가능 여부 확인 필요 |
| 자원 | N100 메모리·CPU 여유 | 병행 검증 Pod 수와 순서 조정 필요 |

## 11. 후속 조치

1. 본 설계서 승인 후 서비스별 구현 계획을 작성함.
2. 첫 구현 범위는 공통 이미지 반입 도구와 Book Memo K3s 전환으로 한정함.
3. 실제 N100 반입·데이터 전환·공개 경로 변경은 테스트·독립 검토·사용자 승인 후에만 수행함.
