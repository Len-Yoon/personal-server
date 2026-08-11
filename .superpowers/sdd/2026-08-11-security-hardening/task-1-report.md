# Task 1 구현 보고서: 포털 세션 및 인증 정책 보강

## 1. 작업 정보

| 항목 | 내용 |
|---|---|
| 작업명 | Task 1: Shared portal session and policy hardening |
| 작업 브랜치 | `codex/security-hardening` |
| 작업 범위 | 포털 파일함·포트폴리오 세션, 인증 실패 제한 저장, 파일함 환경 정책 |
| 제외 범위 | 서버 기동 파일, 스케줄러, Task 2~4 서비스 |
| 커밋 메시지 | `feat: 포털 세션 및 인증 정책 보강` |

## 2. 구현 결과

| 구분 | 적용 내용 | 검증 결과 |
|---|---|---|
| 파일함 세션 | 비밀번호 HMAC 쿠키를 서버 메모리의 임의 토큰 세션으로 변경함 | 일치 |
| 포트폴리오 세션 | 비밀번호 HMAC 쿠키를 서버 메모리의 임의 토큰 세션으로 변경함 | 일치 |
| 세션 제한 | 만료 세션을 정리하고 최대 개수를 초과하면 가장 먼저 만료되는 세션부터 제거함 | 일치 |
| 쿠키 보호 | `HttpOnly`, `SameSite=Lax`을 유지하고 `APP_ENV=production`에서만 `Secure`를 적용함 | 일치 |
| 재시작 처리 | 프로세스 메모리 세션이므로 보안 서비스 재적재 후 기존 파일함·포트폴리오 세션을 거부함 | 일치 |
| 인증 실패 제한 | `AUTH_RATE_LIMIT_STATE_PATH` 우선, 미설정 시 `SECURITY_LOG_PATH` 디렉터리에 JSON을 원자적으로 저장함 | 일치 |
| 파일함 정책 | `APP_ENV=production` 또는 `FILE_MANAGER_AUTH_REQUIRED=true`에서 인증을 강제하고, 모두 비활성인 로컬 개발에서만 비밀번호 없이 접근하도록 반영함 | 일치 |

## 3. TDD 수행 기록

### 3.1 RED

추가한 테스트는 운영 Secure 쿠키, 임의 세션, 재시작 후 세션 무효화, 인증 실패 제한 재적재, 세션 최대 개수, 파일함 개발/운영 정책을 검증함.

```text
$ PYTHONPATH=portal-web python3 -m unittest tests.test_file_access tests.test_portfolio tests.test_portal_security
Ran 26 tests in 0.498s
FAILED (failures=5, errors=1)
```

주요 예상 실패 내용은 다음과 같음.

| 테스트 | RED 원인 |
|---|---|
| `test_production_file_login_uses_secure_server_side_session_cookie` | 파일함 쿠키에 `Secure` 속성이 없음 |
| `test_production_login_sets_random_scoped_session_cookie_and_allows_save` | 포트폴리오 쿠키가 비밀번호 기반 고정 HMAC 값임 |
| `test_portfolio_session_is_rejected_after_security_service_restart` | 재시작 후에도 고정 HMAC 쿠키가 유효함 |
| `test_auth_rate_limit_records_survive_security_service_restart` | 인증 실패 상태 JSON이 생성되지 않음 |
| `test_auth_sessions_evict_oldest_entry_at_configured_bound` | 세션 생성 API 및 최대 개수 제한이 없음 |
| `test_file_manager_policy_allows_passwordless_local_development_only` | 로컬/운영 파일함 인증 정책 분기가 없음 |

### 3.2 GREEN

```text
$ PYTHONPATH=portal-web python3 -m unittest tests.test_file_access tests.test_portfolio tests.test_portal_security
Ran 26 tests in 0.437s
OK
```

포털 대시보드 회귀를 포함한 재검증 결과는 다음과 같음.

```text
$ PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard tests.test_file_access tests.test_portal_security tests.test_portfolio && git diff --check
Ran 45 tests in 0.426s
OK
```

## 4. 변경 파일

| 파일 | 변경 내용 |
|---|---|
| `portal-web/app/services/security.py` | 임의 세션 저장소, 만료/최대 개수 처리, 운영 환경 판별, 인증 실패 JSON 읽기·원자 저장 추가함 |
| `portal-web/app/routers/files.py` | 파일함 HMAC 쿠키 제거, 서버 세션 쿠키 발급/검증, 파일함 환경 정책 반영함 |
| `portal-web/app/routers/portfolio.py` | 포트폴리오 HMAC 쿠키 제거, 서버 세션 쿠키 발급/검증 반영함 |
| `tests/test_file_access.py` | 운영 쿠키, 파일함 재시작 무효화, 개발/운영 인증 정책 테스트 추가함 |
| `tests/test_portfolio.py` | 운영 임의 세션 쿠키 및 재시작 무효화 테스트 추가함 |
| `tests/test_portal_security.py` | 인증 실패 제한 재시작 유지 및 세션 최대 개수 테스트 추가함 |

## 5. 확인 필요 사항 및 우려 사항

| 항목 | 내용 | 조치 |
|---|---|---|
| 전체 테스트 발견 실행 | `PYTHONPATH=portal-web python3 -m unittest discover -s tests`는 crawler-worker 테스트가 동일한 `app` 패키지명을 가져 `app.services.datetime_format` import 오류가 발생함. 145개 실행 중 1개 오류임. | 포털 변경 테스트 45개는 별도 실행하여 통과 확인함. 서비스별 테스트 실행 분리는 별도 개선 필요함. |
| 기존 경고 | Starlette `TemplateResponse` 호출 방식에 대한 DeprecationWarning이 기존 테스트 실행 시 출력됨. | 본 작업 범위 외이며 기능 오류는 아님. 별도 정리 필요함. |
| 세션 재시작 | 세션은 의도적으로 프로세스 메모리에만 보관됨. 서버 재시작 시 사용자는 다시 로그인해야 함. | 설계 결정과 일치함. |

## 6. 후속 조치

- Task 2~4에서 공통 보안 헤더, CSRF, 메모 쓰기 인증 및 환경 문서화를 별도 수행 필요함.
- `AUTH_RATE_LIMIT_STATE_PATH` 운영 경로 설정 여부는 Task 4 문서화 시 확인 필요함.

## 7. 검토 보완 사항

| 검토 항목 | 원인 | 보완 내용 | 검증 결과 |
|---|---|---|---|
| 세션 최대 개수 동시 초과 | 세션 정리·상한 확인·토큰 삽입이 잠금 없이 수행됨 | 프로세스 내 `RLock`으로 전체 발급 및 조회 구간을 직렬화함 | 최대 1개 설정에서 동시 발급 후 유효 세션이 1개임을 확인함 |
| 인증 실패 제한 동시 기록 유실 | 각 인스턴스가 상태를 읽은 뒤 독립적으로 JSON을 교체하여 마지막 기록만 남을 수 있음 | Linux `fcntl.flock` 잠금 파일로 상태 재적재·만료 정리·기록·원자 교체를 하나의 임계 구간으로 처리함 | 5개 별도 프로세스 동시 기록 후 5개 기록이 모두 보존됨을 확인함 |
| 파일함 쿠키 범위 | 기본 쿠키 경로가 포털 전체(`/`)였음 | 파일함 세션 쿠키의 `Path`를 `/files`로 제한함 | 운영 쿠키 속성 테스트로 확인함 |

### 7.1 검토 보완 TDD 기록

```text
# RED
$ PYTHONPATH=portal-web python3 -m unittest \
  tests.test_file_access.FileAccessTests.test_production_file_login_uses_secure_server_side_session_cookie \
  tests.test_portal_security.PortalSecurityTests.test_auth_session_cap_holds_during_concurrent_session_creation \
  tests.test_portal_security.PortalSecurityTests.test_auth_rate_limit_keeps_all_concurrent_process_failures
Ran 3 tests in 0.638s
FAILED (failures=3)

# GREEN
$ PYTHONPATH=portal-web python3 -m unittest \
  tests.test_file_access.FileAccessTests.test_production_file_login_uses_secure_server_side_session_cookie \
  tests.test_portal_security.PortalSecurityTests.test_auth_session_cap_holds_during_concurrent_session_creation \
  tests.test_portal_security.PortalSecurityTests.test_auth_rate_limit_keeps_all_concurrent_process_failures
Ran 3 tests in 0.605s
OK

# Portal regression
$ PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard tests.test_file_access tests.test_portal_security tests.test_portfolio && git diff --check
Ran 47 tests in 0.934s
OK
```

## 8. 재검토 보완 사항

| 검토 항목 | 원인 | 보완 내용 | 검증 결과 |
|---|---|---|---|
| 동시 로그인 사전 제한 우회 | 라우터가 `auth_rate_limited()`와 `record_auth_failure()`를 별도 잠금 트랜잭션으로 호출하여, 여러 요청이 모두 제한 전 상태를 확인할 수 있었음 | `record_auth_failure()`가 동일 잠금 안에서 최신 상태를 재확인하고 이미 제한된 경우 `True`를 반환하도록 변경함. 파일함 로그인·삭제, 포트폴리오 로그인, 관리자 상태 인증이 해당 결과를 즉시 429로 처리하도록 반영함 | 동일 IP 6개 동시 파일함 잘못된 로그인에서 403 5건, 429 1건을 확인함 |

### 8.1 재검토 TDD 기록

```text
# RED
$ PYTHONPATH=portal-web python3 -m unittest tests.test_file_access.FileAccessTests.test_concurrent_failed_logins_reject_attempts_after_rate_limit
Ran 1 test in 0.144s
FAILED (failures=1)
# 실제 결과: 403 6건, 429 0건

# GREEN
$ PYTHONPATH=portal-web python3 -m unittest tests.test_file_access.FileAccessTests.test_concurrent_failed_logins_reject_attempts_after_rate_limit
Ran 1 test in 0.146s
OK

# Portal regression
$ PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard tests.test_file_access tests.test_portal_security tests.test_portfolio && git diff --check
Ran 48 tests in 0.849s
OK
```
