# Task 4 구현 보고

## 상태

Portal 관리자 상태의 읽기 전용 자동복구 이력 표시를 구현하고 로컬 검증함. 실제 N100 외부 health 검증과 독립 운영·보안 검토는 통합 단계에서 수행 필요함.

## 변경 내용

- 인증이 완료된 `/admin/status`에서만 system-agent bridge의 `GET /recovery-events`를 조회하도록 추가함.
- Portal에서도 `timestamp`, `component`, `event`, `status`, `action` 다섯 허용 필드만 다시 구성해 원본의 오류·경로·비밀값 관련 추가 필드를 표시하지 않도록 함.
- 최근 최대 10건을 KST `YYYY-MM-DD HH:MM` 형식으로 표에 표시함.
- 화면에 `accepted`는 조치 실행 수락이고 `health_restored`만 실제 복구 완료임을 명시함.
- 인증 응답의 기존 `no-store` 캐시 정책과 HomeOps 세션 인증 흐름을 유지함.

## TDD 및 검증

- RED: `test_authenticated_admin_status_displays_only_sanitized_recent_recovery_events`를 먼저 추가하고 실행함. bridge 조회 mock이 0회 호출되어 예상대로 실패함.
- GREEN: bridge 조회, 정제 표시 모델, 관리자 표를 추가한 뒤 같은 테스트 통과함.
- 관련 Portal 테스트: `python3 -m unittest tests.test_portal_dashboard` 실행 결과 26건 통과함.
- 관련 관리자 회귀 테스트: `python3 -m unittest tests.test_homeops` 실행 결과 36건 통과함.
- 변경 범위 정책 테스트: `python3 -m unittest tests.test_verify_change_scope` 실행 결과 35건 통과함.
- 정적 검사: `python3 -m compileall -q portal-web/app`, `git diff --check` 통과함.
- 변경 범위 검증: `portal=success`, `maintenance=success` 입력 후 change harness 상태 `ready_for_review` 확인함.

## 제외 및 확인 필요 사항

- Portal은 Windows host 로그를 직접 mount·읽기하지 않음.
- PVC, Secret, Caddy, Compose writer, 서버 기동·스케줄러, 자격증명은 변경하지 않음.
- 실제 N100에서 bridge 응답과 외부 `https://len.pe.kr/health` 3회 검증은 통합 운영 검토에서 수행 필요함.
