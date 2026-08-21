# 개인서버 에이전트 작업 루프 CI 게이트 설계

## 1. 문서 개요

| 항목 | 내용 |
|---|---|
| 문서명 | 개인서버 에이전트 작업 루프 CI 게이트 설계 |
| 작성일 | 2026-08-21 |
| 작성자 | Codex |
| 기준 자료 | `AGENTS.md`, `docs/codex-work-loop.md`, `.github/workflows/ci.yml`, `.github/workflows/deploy-n100.yml` |
| 목적 | Codex 작업 완료 루프의 검증·중단·검토 기준을 GitHub Actions CI와 연결함 |
| 비고 | 개인서버 저장소에만 적용하며, 자동 코드 수정·자동 배포는 포함하지 않음 |

## 2. 핵심 요약

- 변경 파일을 기준으로 영향 서비스를 판정하고, 서비스별 필수 검증 목록을 생성함.
- 기존 CI의 서비스별 단위 테스트는 유지하고, 변경 범위와 검증 기준을 CI 결과 요약 및 아티팩트로 남김.
- PR 전용 독립 검토 workflow에서 금지 영역 변경, 검증 범위 누락, 문서 참조 누락을 검사함.
- 검사 실패 시 CI를 중단하며, 실패 원인과 필요한 검증을 GitHub Actions 결과에서 확인할 수 있게 함.
- 자동 수정, 자동 재시도, 자동 병합, 배포 workflow 변경은 제외함.

## 3. 현재 상태

| 구분 | 현재 상태 | 보완 방향 |
|---|---|---|
| 작업 규칙 | `AGENTS.md`와 상세 루프 문서가 존재함 | CI가 규칙을 확인하도록 연결 필요 |
| 단위 테스트 | 서비스별 matrix CI가 존재함 | 변경 범위별 필수 검증 기록 필요 |
| 배포 | CI 성공 후 N100 health check를 수행함 | 변경하지 않음 |
| 독립 검토 | PR 전용 변경 범위 검토 없음 | 정적 정책 검토 workflow 추가 |
| 증거 기록 | CI 로그 중심 | 검증 범위·결과를 summary 및 artifact로 구조화 |

## 4. 목표 구조

```text
작업 요청
→ Codex 작업 완료 루프 적용
→ 변경 파일 수집
→ 영향 서비스·필수 검증 판정
→ 기존 단위 테스트 실행
→ 검증 범위·결과 요약 저장
→ PR 독립 검토
→ 성공: 기존 CI 성공 조건 유지
→ 실패: 중단 및 원인 확인
```

## 5. 구성 요소

| 구성 요소 | 파일 | 역할 |
|---|---|---|
| 변경 범위 검사기 | `scripts/verify_change_scope.py` | 변경 파일을 서비스·공통 설정·금지 영역으로 분류하고 필수 검증 목록을 JSON으로 출력함 |
| 검사기 테스트 | `tests/test_verify_change_scope.py` | 서비스별·공통 파일·금지 영역 변경의 분류 결과를 검증함 |
| CI 검증 기록 | `.github/workflows/ci.yml` | 변경 범위 검사 결과, 서비스별 테스트 결과, 검증 요약을 workflow summary와 artifact로 기록함 |
| PR 독립 검토 | `.github/workflows/agent-review.yml` | PR 변경에서 금지 영역, 범위 미분류, 검증 문서 누락을 확인함 |
| 증거 운영 문서 | `docs/agent-loop-evidence.md` | CI 결과 해석, 실패 분류, 개선 이력 기록 기준을 정의함 |
| README 안내 | `README.md` | 적용 범위와 사실 기반 운영 방식을 요약함 |

## 6. 변경 범위 판정 기준

| 변경 경로 | 분류 | 필수 검증 | 비고 |
|---|---|---|---|
| `portal-web/**` | portal | portal 단위 테스트 | 포털·파일·관리자·HomeOps 라우트 포함 |
| `system-agent/**` | system-agent | system-agent 단위 테스트 | 호스트 상태 API |
| `crawler-worker/**` | crawler-worker | crawler-worker 단위 테스트 | 스케줄러 코드는 수정 금지 여부 별도 확인 필요 |
| `homeops-executor/**` | homeops-executor | homeops-executor 단위 테스트 | 제한된 Docker 실행기 |
| `youtube-memo/**` | youtube-memo | youtube-memo 단위 테스트 | 메모 서비스 |
| `book-memo/**` | book-memo | book-memo 단위 테스트 | 독서 메모 서비스 |
| `docker-compose*.yml`, `scripts/**`, `caddy/**` | infrastructure | maintenance 단위 테스트 및 수동 확인 필요 | 서버 기동·배포 스크립트 변경은 자동 승인하지 않음 |
| `AGENTS.md`, `docs/**`, `README.md` | documentation | Markdown 구조 검사 및 문서 diff 검토 | 코드 테스트는 필수 아님 |
| 분류되지 않은 경로 | unclassified | 중단 | 사용자 확인 필요 |

## 7. 금지·중단 기준

| 구분 | 조건 | 처리 |
|---|---|---|
| 서버 기동 영역 | Compose 기동, 배포, Caddy 설정 변경이 감지됨 | 검토 실패 및 수동 확인 필요로 기록함 |
| 스케줄러 영역 | `crawler-worker/app/services/news_scheduler.py` 변경이 감지됨 | 검토 실패 및 사용자 확인 필요로 기록함 |
| 미분류 변경 | 판정 규칙에 없는 경로가 감지됨 | 검토 실패 및 분류 규칙 추가 또는 사용자 확인 요청 |
| 검증 누락 | 코드 변경 서비스에 필요한 테스트 결과가 없음 | 검토 실패 |
| 문서만 변경 | 코드 테스트를 요구하지 않음 | Markdown·참조·diff 검사 결과를 기록함 |

## 8. CI 동작 설계

1. CI 시작 시 PR 또는 push의 base와 head 사이 변경 파일을 수집함.
2. 변경 범위 검사기가 분류 결과와 필수 검증 목록을 JSON으로 생성함.
3. 기존 서비스별 unit test matrix는 유지함.
4. 각 matrix job은 서비스별 성공·실패 결과를 artifact로 업로드함.
5. 집계 job은 변경 범위, 요구 검증, 실행 결과, 누락 여부를 GitHub Step Summary 및 `agent-loop-evidence.json` artifact에 기록함.
6. PR에서 `agent-review.yml`은 금지·미분류·검증 누락 조건을 독립적으로 확인함.
7. 하나라도 실패하면 workflow는 실패하며, 배포 workflow는 기존 조건에 따라 시작되지 않음.

## 9. 증거 및 README 기준

- `docs/agent-loop-evidence.md`에는 CI 검증 결과, 실패 분류, 재시도 횟수, 사람 확인 여부를 기록하는 양식을 정의함.
- README에는 “문서화된 작업 완료 루프와 CI 검증 게이트를 적용함”이라는 사실만 기재함.
- “완전 자율”, “무인 자동 수정”, “모든 변경을 자동 검증”처럼 현재 구현 범위를 초과하는 표현은 사용하지 않음.

## 10. 제외 범위

- `scripts/deploy-n100.sh`, `crawler-worker/app/services/news_scheduler.py`, Compose 서비스 기동 동작은 변경하지 않음.
- GitHub Actions에서 Codex 또는 외부 LLM을 호출해 자동 수정·리뷰 댓글·재푸시하지 않음.
- 자동 병합, 자동 배포, 자동 롤백, 외부 알림 연동은 포함하지 않음.
- CI가 통과하지 못한 기존 테스트의 원인 수정은 범위에 포함하지 않음.

## 11. 검증 기준

| 검증 항목 | 기준 |
|---|---|
| 범위 판정 단위 테스트 | 서비스·문서·금지·미분류 경로가 설계대로 분류됨 |
| CI workflow 구조 검사 | 변경 범위 검사, evidence artifact, 집계 job이 존재함 |
| PR 검토 workflow 구조 검사 | 금지 영역·미분류·검증 누락 검사를 수행함 |
| 기존 CI 회귀 | 기존 서비스별 unit test matrix 구성이 유지됨 |
| 문서 검증 | README와 증거 문서의 표현이 실제 구현 범위를 초과하지 않음 |

## 12. 후속 조치

1. 구현 계획을 작성하고 파일별 변경·테스트 순서를 확정함.
2. 변경 범위 검사기와 단위 테스트를 구현함.
3. CI evidence 기록과 PR 독립 검토 workflow를 추가함.
4. 문서와 README를 업데이트함.
5. 실제 PR 또는 테스트 브랜치에서 결과 artifact와 중단 기준을 확인함.
