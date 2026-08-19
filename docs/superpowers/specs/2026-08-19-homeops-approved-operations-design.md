# HomeOps 정책 기반 Docker 운영 보조 설계

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | HomeOps 정책 기반 Docker 운영 보조 설계 |
| 기준일 | 2026-08-19 |
| 기준 자료 | `portal-web`, `homeops-executor`, Docker Compose, Windows bootstrap |
| 적용 상태 | 저장소 `main` 기준 구현 완료. N100 반영 여부는 배포 workflow 결과 확인 필요 |
| 목적 | Docker 컨테이너의 제한된 진단·재시작·복구 검증·이력 저장 구조 정의 |

## 2. 핵심 요약

- 현재 HomeOps는 외부 AI가 아닌 **결정 가능한 규칙 기반** 운영 보조임. AI API·모델·자유 명령 실행 기능은 구현하지 않음.
- `portal-web`은 진단 결과, 관리자 수동 승인, 자동복구 정책, SQLite 이력을 담당함.
- Docker 소켓은 외부 포트가 없는 `homeops-executor`에만 부여함. 허용된 Compose 서비스의 상태·제한 로그 조회와 `restart_container`만 제공함.
- 관리자는 관리자 상태 화면에서 진단 후 승인하여 재시작할 수 있음. 정기 점검은 연속 장애 3회를 확인한 경우에만 정책 승인으로 컨테이너 재시작을 시도함.
- Windows·WSL·Docker 엔진·컨테이너 전체 재기동, 파일·방화벽·SSH·DNS 변경, 임의 셸 명령은 수행하지 않음.

## 3. 적용 범위

| 구분 | 현재 적용 내용 |
|---|---|
| 대상 서비스 | `portal-web`, `system-agent`, `crawler-worker`, `youtube-memo`, `book-memo`, `caddy`, `homeops-executor` |
| 읽기 진단 | 컨테이너 상태, health 상태, CPU·메모리 사용량, 최근 제한 로그 |
| 허용 조치 | allowlist 대상의 `restart_container` 1종 |
| 수동 흐름 | 관리자 상태 로그인 → 진단 → 승인 → 실행 → 복구 검증 → 이력 조회 |
| 정기 흐름 | Windows bootstrap이 5분마다 내부 점검 API 호출 → 정책 평가 → 조건 충족 시 컨테이너만 재시작 |
| 알림 | 재시작 시작, 복구 성공·실패, host 메모리 이상·정상화만 HomeOps Telegram 채팅방으로 전송 |
| 제외 항목 | 외부 AI 분석, 임의 명령, 호스트 재부팅, Docker 엔진 재시작, 네트워크·보안 설정 변경 |

## 4. 구성과 권한 경계

```text
관리자 ── 관리자 상태 화면 ── portal-web HomeOps 조정 계층
                                    ├─ HomeOps SQLite 이력·승인 상태
                                    ├─ 규칙 기반 진단·자동복구 정책
                                    └─ 내부 HTTP + 공유 비밀값
                                              │
                                      homeops-executor ── Docker socket
```

| 구성요소 | 책임 | 직접 수행하지 않는 작업 |
|---|---|---|
| `system-agent` | host metrics와 기존 읽기 전용 상태 API 제공 | Docker 제어 |
| `portal-web` | 관리자 인증, 진단 조정, 정책 판단, 승인·이력·Telegram 알림 | Docker 소켓 접근, 셸 실행 |
| `homeops-executor` | allowlist 서비스의 진단·컨테이너 재시작 | 자유 Docker API, 컨테이너 생성·삭제, 셸 실행 |
| Windows bootstrap | host metrics 기록, Compose/Tunnel 확인, 내부 HomeOps 정기 점검 호출 | Windows·WSL·Docker 엔진 재시작 |

## 5. 동작 흐름

### 5.1 관리자 수동 조치

```text
진단 요청 → proposed 이력 생성 → 관리자 승인 → executing → verified 또는 failed
```

1. 관리자가 관리자 상태 화면에서 서비스를 진단함.
2. 상태·health·자원 사용량·최근 로그로 조치 필요성을 평가하고 `proposed` 이력을 저장함.
3. 관리자가 승인하면 단일 사용 승인 토큰을 발급함.
4. `homeops-executor`가 공유 비밀값을 검증한 뒤 allowlist의 `restart_container`만 호출함.
5. 최대 5회, 2초 간격으로 컨테이너의 실행·health 상태를 확인하여 `verified` 또는 `failed`로 이력을 갱신함.

### 5.2 정기 자동복구

```text
5분 정기 점검 → 서비스별 진단 → 3회 연속 unhealthy
→ 정책 승인 → 컨테이너 재시작 → 복구 검증·알림·이력
```

- 정기 점검의 정상 결과는 이력을 저장하지 않아 SQLite 이력과 Telegram 채널의 잡음을 줄임.
- 장애 조건이 연속 3회 확인되어야 자동 재시작 후보가 됨.
- 자동복구 뒤에는 서비스별 10분 쿨다운을 적용하고, 최근 1시간 자동 재시작은 최대 2회로 제한함.
- 제한 도달 시에는 재시작하지 않고 이력과 Telegram 알림만 남김.

## 6. 판정·복구 정책

| 항목 | 현재 값 |
|---|---|
| 즉시 비정상 | 컨테이너가 실행 중이 아님 또는 Docker health가 `unhealthy` |
| 자원 기반 비정상 | CPU 85% 이상 또는 컨테이너 메모리 한도의 90% 이상이며, 같은 진단에 치명 로그가 존재할 때 |
| 치명 로그 예시 | `fatal`, `panic`, `oom`, `out of memory`, `memoryerror`, `segmentation fault`, `connection refused` |
| 자동복구 진입 | 동일 서비스가 3회 연속 비정상 |
| 자동복구 조치 | 해당 컨테이너만 재시작 |
| 복구 확인 | 실행·health 상태를 최대 5회, 2초 간격으로 확인 |
| host 메모리 | Windows host metrics 90% 이상이 3회 연속이면 알림만 전송. 호스트 재시작 없음 |

## 7. 저장 이력과 알림

| 항목 | 내용 |
|---|---|
| 저장소 | `HOMEOPS_DB_PATH`의 SQLite |
| 주요 데이터 | incident 상태, 진단 근거, 승인 토큰 사용 상태, 재시작 결과, 복구 검증 결과, 알림 상태 |
| 상태 | `proposed` → `approved` → `executing` → `verified` 또는 `failed` |
| 정책 승인 표시 | 자동복구는 승인 주체를 `homeops-policy`로 기록함 |
| Telegram | `HOMEOPS_TELEGRAM_BOT_TOKEN`, `HOMEOPS_TELEGRAM_CHAT_ID`가 설정된 경우에만 전송함 |
| 알림 실패 | 알림 전송 실패는 진단·복구 흐름을 중단하지 않음 |
| 화면 시각 | SQLite에는 UTC로 저장하고, 관리자 화면 이력에는 KST로 표시함 |

## 8. 보안 통제와 현재 제한

- HomeOps 화면과 상태 변경 API는 기존 관리자 인증과 Origin 검증 경계를 사용함.
- 실행기는 외부 포트를 열지 않으며, 내부 `HOMEOPS_EXECUTOR_SHARED_SECRET` 헤더가 일치해야 요청을 처리함.
- 실행 입력은 서비스명과 `restart_container` enum으로 제한함. 컨테이너 ID·Docker API 경로·명령문은 입력으로 받지 않음.
- Docker 소켓은 높은 호스트 권한을 가지므로 executor의 코드·이미지·Compose 설정 변경은 보안 변경으로 검토 필요함.
- 현재 승인 토큰은 `portal-web`이 SQLite에서 단일 사용으로 소비함. executor는 내부 공유 비밀값과 allowlist·action만 독립 검증함. **executor가 승인 토큰을 직접 검증하지 않는 것은 현 구현의 제한 사항**임.
- 로그 원문에 민감값이 포함될 수 있으므로 HomeOps 이력·관리자 접근 권한을 운영 중 관리해야 함.

## 9. 하네스 루프 관점

| 계층 | 구현 요소 | 역할 |
|---|---|---|
| 관측 | 컨테이너 진단, host metrics, 제한 로그 | 상태를 구조화하여 수집함 |
| 판단 | 임계값, 치명 로그, 연속 횟수, 쿨다운·시간당 한도 | 재시작 필요성을 재현 가능하게 판단함 |
| 조치 | 승인된 `restart_container` | 허용된 영향 범위 안에서만 변경함 |
| 검증 | 5회 health·실행 상태 확인 | 조치 결과를 관측값으로 되돌림 |
| 학습 자료 | SQLite incident 이력, Telegram 알림 | 이후 임계값·정책을 조정할 근거를 남김 |

외부 AI 연동은 향후 선택 사항임. 추가하더라도 AI 출력은 설명·우선순위 제안으로 제한하고, 현재의 allowlist·승인·정책·복구 검증 경계를 우회해서는 안 됨.

## 10. 운영 확인 및 확인 필요 사항

- Compose 반영 상태는 `docker compose -f docker-compose.yml -f docker-compose.n100.yml ps`로 확인함.
- 관리자 화면에서 HomeOps 진단·승인·이력과 기존 컨테이너 상태를 함께 확인함.
- 정기 점검 호출에는 `HOMEOPS_SCHEDULER_SECRET`이 필요하며, 값이 비어 있으면 자동복구가 실행되지 않음.
- N100 실제 반영은 `main` push 후 GitHub Actions `Deploy N100` 성공 여부와 N100의 Compose 상태로 별도 확인 필요함.
- 외부 AI 도입, executor의 승인 토큰 독립 검증, 이력 보존 기간은 후속 설계·검증 필요 사항임.
