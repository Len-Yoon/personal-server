# HomeOps 승인 기반 AI 운영 보조 시스템 설계

## 1. 목적

N100 개인 서버에서 컨테이너 장애를 안전하게 진단하고, AI가 제안한 조치안을 관리자가 승인한 경우에만 제한적으로 실행하는 HomeOps 시스템을 구현함.

## 2. 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | HomeOps 승인 기반 AI 운영 보조 시스템 설계 |
| 작성일 | 2026-08-19 |
| 기준 자료 | `system-agent`, `portal-web` 관리자 상태, Docker Compose, N100 운영 문서 |
| 목적 | AI 운영 보조의 권한 분리, 승인 절차, 검증 및 이력 구조 정의 |
| 비고 | 1단계는 Docker 컨테이너 진단·로그 분석·승인 기반 재시작으로 한정함 |

## 3. 핵심 요약

- AI는 진단 자료를 해석하고 구조화된 조치안을 생성하되, 명령문 또는 실행 권한을 가지지 않음.
- `system-agent`는 읽기 전용 진단 책임만 유지하며 Docker 제어 권한을 부여하지 않음.
- 별도 HomeOps 제한 실행기가 승인된 요청을 검증한 후, 허용 목록의 컨테이너에 대해 `restart`만 실행함.
- 실행 성공은 Docker 응답만으로 판정하지 않고 컨테이너 상태, health endpoint, 최근 로그 재확인으로 검증함.
- 모든 단계의 입력·결과·실패 원인을 append-only 장애 이력으로 저장함.

## 4. 현행 구조와 적용 경계

| 영역 | 현행 책임 | HomeOps 적용 |
|---|---|---|
| `system-agent` | host metrics, 백업, 디스크, 기대 컨테이너 목록 조회 | 기존 읽기 전용 상태 API 유지. Docker 소켓 미부여 |
| `portal-web` 관리자 상태 | 비밀번호 인증 후 상태·보안 정보 표시 | HomeOps 진단, 조치안, 승인, 이력 UI 추가 |
| Docker Compose | 서비스 실행 및 N100 오버레이 | HomeOps 전용 서비스의 최소 권한 구성만 추가 |
| N100 배포·Windows bootstrap·스케줄러 | 배포·기동·정기 복구 | 변경 및 호출 대상에서 제외 |

## 5. 범위

### 5.1 포함 범위

- Compose 관리 대상 컨테이너의 상태, health, 종료 코드, 최근 제한 로그 조회
- 진단 결과를 AI에 전달하고 JSON 스키마의 조치안 수신
- 관리자 상태 화면에서 조치안, 근거, 위험도, 검증 기준 표시
- 관리자 1회 승인 후 허용된 컨테이너 `restart` 실행
- 실행 뒤 상태, health endpoint, 로그 기반 복구 검증
- 진단·승인·실행·검증 이력 저장 및 조회
- `crawler-worker`만 최초 재시작 허용 목록에 등록

### 5.2 제외 범위

- 파일 삭제·수정, 이미지 빌드·pull, 컨테이너 생성·삭제, Compose up/down
- 방화벽, SSH, DNS, Cloudflare, Caddy, Windows, WSL 설정 변경
- 임의 셸 명령, AI가 생성한 명령의 직접 실행
- `system-agent`, `caddy`, 배포 스크립트, Windows bootstrap, 스케줄러 재시작
- 로컬 LLM 또는 N100 내 GPU/모델 상시 실행
- 자동 승인 및 무인 자동복구

## 6. 설계 대안 및 결정

| 대안 | 장점 | 위험 및 한계 | 결정 |
|---|---|---|---|
| `system-agent`에 Docker 소켓 부여 | 구현 단순 | 읽기 전용 서비스의 권한 경계가 무너짐 | 제외 |
| `portal-web`이 Docker를 직접 제어 | 화면과 실행 흐름 단순 | 공개 웹 애플리케이션에 Docker 제어 권한 집중 | 제외 |
| 별도 제한 실행기와 승인 토큰 | 진단·UI·실행 권한 분리, 허용 동작 강제 가능 | 서비스 하나 추가 필요 | 채택 |

## 7. 아키텍처

```text
관리자 ── 관리자 상태 화면 ── HomeOps 조정 API
                                  ├─ 읽기 전용 진단 수집기
                                  ├─ AI 제안 어댑터
                                  ├─ 승인·정책·이력 저장소
                                  └─ 제한 실행기 ── Docker API
```

### 7.1 책임 분리

| 구성요소 | 책임 | 금지 사항 |
|---|---|---|
| 진단 수집기 | 제한 실행기의 읽기 전용 진단 응답을 정규화해 반환 | Docker 상태 변경, 파일 쓰기 |
| AI 제안 어댑터 | 진단 입력으로 구조화된 조치안 생성 | 명령문 반환, Docker 호출 |
| HomeOps 조정 API | 정책 검증, 승인 상태 전이, 이력 기록 | Docker 소켓 접근 |
| 제한 실행기 | 승인된 enum 요청의 재시작·검증 실행 | 셸 실행, 자유 입력, allowlist 외 작업 |
| 관리자 UI | 진단 확인, 승인·거절, 이력 열람 | 승인 없는 실행 |

### 7.2 Docker 권한 경계

제한 실행기만 Docker 통신 권한을 가짐. 실행기는 외부 포트를 열지 않고 HomeOps 조정 API가 있는 내부 Docker 네트워크에서만 요청을 받음. 실행기는 고정된 서비스 목록의 상태·health·제한 로그 조회와 `restart_container`만 제공하며, 컨테이너 ID, Docker API 경로, 명령 문자열을 입력으로 받지 않음.

Docker 소켓은 높은 호스트 권한을 가지므로, 실행기 코드는 Docker SDK의 restart 호출 외 API를 노출하지 않음. 이후 구현 시 가능하면 Docker API 요청을 `GET /containers/*`, `GET /containers/*/logs`, `POST /containers/{allowlist-name}/restart`로 제한하는 전용 프록시 또는 호스트 브로커를 추가 검토함.

## 8. 상태 모델과 데이터 흐름

### 8.1 상태 전이

```text
diagnosed → proposed → approved | rejected | expired
approved → executing → verified | failed
```

- `diagnosed`: 읽기 전용 진단 결과가 저장됨.
- `proposed`: AI 조치안이 스키마 및 정책 검증을 통과함.
- `approved`: 관리자가 1회 승인 토큰을 발급함.
- `executing`: 제한 실행기가 토큰을 소비하고 재시작을 수행 중임.
- `verified`: 컨테이너와 서비스 health가 복구 기준을 통과함.
- `failed`: 실행 또는 복구 검증에 실패함.
- `rejected`, `expired`: 실행 없이 종료됨.

### 8.2 처리 절차

1. 관리자가 대상 서비스의 진단을 요청함.
2. 수집기는 상태, health, 최근 로그를 읽어 진단 스냅샷으로 저장함.
3. AI 어댑터는 스냅샷만 입력받아 아래 조치안 스키마를 반환함.
4. 조정 API는 action, service, 위험도, 근거 필드를 검증하고 허용 목록 정책을 적용함.
5. 관리자는 UI에서 근거·영향·검증 기준을 확인하고 승인 또는 거절함.
6. 승인 시 만료 시간이 짧고 단 한 번만 사용할 수 있는 승인 토큰을 발급함.
7. 제한 실행기는 토큰, 상태, 서비스 allowlist, action enum을 모두 재검증한 후 재시작함.
8. 실행기는 상태, 지정 health endpoint, 제한 로그를 재확인함.
9. 조정 API는 실행 및 검증 결과를 장애 이력에 추가하고 관리자 화면에 표시함.

## 9. 계약 정의

### 9.1 진단 레코드

| 필드 | 형식 | 설명 |
|---|---|---|
| `incident_id` | UUID | 장애·조치 흐름 식별자 |
| `service` | enum | Compose 서비스명 |
| `observed_at` | ISO-8601 UTC | 진단 시각 |
| `container` | object | 상태, health, exit code, 시작 시각 |
| `health_check` | object | HTTP 확인 결과와 오류 내용 |
| `logs` | string array | 줄 수·바이트 수가 제한된 최근 로그 |
| `evidence_hash` | SHA-256 | 원본 스냅샷 무결성 확인값 |

### 9.2 AI 조치안 스키마

```json
{
  "schema_version": 1,
  "incident_id": "uuid",
  "summary": "진단 요약",
  "suspected_causes": ["근거가 있는 원인 후보"],
  "evidence": ["진단 로그 또는 상태의 참조"],
  "risk_level": "low|medium|high",
  "action": "restart_container|no_action",
  "service": "crawler-worker",
  "expected_impact": "재시작 중 일시적 뉴스 수집 중단",
  "verification": ["container_running", "http_health_ok", "recent_error_absent"],
  "requires_approval": true
}
```

검증 실패, JSON 파싱 실패, `service` 불일치, allowlist 밖 서비스, `restart_container` 외 action은 `no_action`으로 격하하고 실행 불가 이력으로 저장함.

### 9.3 실행 요청 계약

```json
{
  "incident_id": "uuid",
  "approval_token": "random one-time token",
  "action": "restart_container",
  "service": "crawler-worker"
}
```

제한 실행기는 승인 토큰의 해시, 만료, 사용 여부, incident 상태, action과 service 일치를 확인함. 검증 결과가 실패하면 재시도 또는 다른 조치를 수행하지 않음.

## 10. 정책

| 정책 항목 | 1단계 값 |
|---|---|
| 허용 서비스 | `crawler-worker` |
| 허용 action | `restart_container` |
| 자동 실행 | 금지 |
| 승인 토큰 | 단일 사용, 짧은 만료시간 |
| 로그 수집 | 최근 N줄 및 최대 바이트 수 제한, 비밀값 마스킹 |
| AI 실패 | 조치 없음, 관리자에게 오류 표시, 이력 저장 |
| 검증 실패 | 실패 이력 저장, 후속 자동 조치 금지 |
| 장애 이력 | append-only JSONL 또는 SQLite, 보존 정책은 별도 설정 |

## 11. 보안 및 오류 처리

- 모든 HomeOps 상태 변경 요청은 기존 관리자 인증 및 Origin 검증 정책을 통과해야 함.
- 승인 화면과 이력 화면은 `Cache-Control: no-store`를 유지함.
- 진단 원본과 AI 응답은 HTML 이스케이프해 표시하며, 로그에 포함된 토큰·비밀번호·Authorization 값은 저장 전에 마스킹함.
- HomeOps API·실행기는 인터넷에 직접 포트를 공개하지 않음.
- 동시 승인은 incident별 잠금으로 직렬화하고, 이미 소비된 승인 토큰을 거부함.
- `restart_container` 중인 동일 서비스에는 새 승인 요청을 생성하지 않음.
- Docker, AI 제공자, health endpoint 중 하나라도 사용할 수 없으면 실행을 중단하고 실패 상태와 원인을 기록함.

## 12. 성능 및 자원 기준

- AI 모델은 N100에서 실행하지 않고 외부 API를 호출함.
- HomeOps 조정 API 및 제한 실행기는 각각 메모리 제한을 두며, 1단계 총 상한은 128MB 이하를 목표로 함.
- 상시 폴링을 추가하지 않고 관리자 요청 또는 명시적 진단 요청에만 로그·상태를 수집함.
- 대용량 로그 전체를 메모리에 적재하지 않음.

## 13. 검증 기준

- 비승인, 만료, 재사용 승인 토큰으로 재시작할 수 없음.
- `crawler-worker` 외 서비스 및 `restart_container` 외 action은 실행되지 않음.
- AI가 명령문·허용되지 않은 action을 반환해도 Docker 호출이 발생하지 않음.
- 재시작 성공 뒤 컨테이너 실행 상태와 `/health`가 모두 정상일 때만 `verified`가 됨.
- health 또는 로그 검증 실패 시 `failed` 이력과 근거가 저장됨.
- Docker·AI 연결 실패, 로그 마스킹, 동시 승인, 관리자 인증·Origin 방어에 대한 자동 테스트가 통과함.
- 기존 portal-web, system-agent, Compose, N100 배포 관련 테스트가 회귀 없이 통과함.

## 14. 확인 필요 사항

- AI 제공자, 모델, API 키 보관 방식은 별도 결정 필요함.
- Docker 소켓 프록시와 WSL 호스트 브로커 중 최종 권한 격리 방식은 N100 환경에서 검증 필요함.
- 장애 이력의 SQLite 사용 여부와 보존 기간은 운영량 확인 후 결정 필요함.
- `crawler-worker` 재시작이 뉴스 수집 중인 작업을 중단하는 것이 허용되는지 운영 확인 필요함.

## 15. 후속 조치

1. 본 설계의 Docker 권한 격리 방식과 최초 allowlist를 검토함.
2. 승인 후 TDD 기준의 구현 계획을 작성함.
3. 구현은 읽기 전용 진단과 정책 테스트부터 시작함.
