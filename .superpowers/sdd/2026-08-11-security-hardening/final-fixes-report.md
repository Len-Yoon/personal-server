# 보안 보강 최종 검토 보완 보고서

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | 보안 보강 최종 검토 보완 보고서 |
| 작성일 | 2026-08-11 |
| 기준 자료 | `.superpowers/sdd/2026-08-11-security-hardening/final-review.md` |
| 대상 범위 | Book Memo·YouTube Memo 인증 실패 제한, portal-web unsafe 요청 Origin 검증 |
| 제외 범위 | 서버 기동 및 스케줄러 코드 |

## 2. 핵심 요약

최종 검토에서 확인된 Important 2건을 보완함.

| 검토 항목 | 보완 결과 | 검증 결과 |
|---|---|---|
| Book Memo·YouTube Memo 인증 실패 제한 | 서비스별 데이터 경로의 JSON 상태 파일, 프로세스 간 파일 잠금, 최신 상태 재적재, 만료 정리, `fsync` 및 원자적 파일 교체 적용함 | 재시작 후 차단 유지 및 4개 프로세스 기록 보존 확인함 |
| portal-web CSRF 방어 | `len.pe.kr`, `portfolio.len.pe.kr`, `file.len.pe.kr`, `admin.len.pe.kr`의 HTTPS Origin 기준으로 unsafe 요청 거부 처리 적용함 | 세션 보유 상태의 `/files/folders`, `/admin/save` 교차 Origin 403 및 동일 Origin 303 확인함 |

## 3. 상세 보완 내용

### 3.1 메모 서비스 인증 실패 제한

| 항목 | 내용 |
|---|---|
| 대상 파일 | `book-memo/app/main.py`, `youtube-memo/app/main.py` |
| 저장 경로 | `AUTH_RATE_LIMIT_STATE_PATH` 우선 사용, 미설정 시 각 서비스 SQLite DB와 동일한 데이터 디렉터리의 `auth-rate-limit-state.json` 사용 |
| 동시성 처리 | lock 파일에 `fcntl.flock(LOCK_EX)` 적용 후 상태 파일 재적재·변경·저장을 하나의 임계 구역으로 처리함 |
| 내구성 처리 | JSON 저장 파일 `flush`·`fsync` 후 `os.replace`로 교체함 |
| 제한 처리 | 이미 제한된 클라이언트가 동시 실패를 기록하려는 경우 즉시 429 반환하도록 변경함 |
| 만료 처리 | 모든 조회·기록·해제 시 300초 기본 창 밖의 기록을 제거하고 변경 시 저장함 |

### 3.2 portal-web Origin 검증

| 항목 | 내용 |
|---|---|
| 대상 파일 | `portal-web/app/main.py` |
| 적용 메서드 | `GET`, `HEAD`, `OPTIONS`, `TRACE` 이외의 모든 메서드 |
| 허용 기준 | 브라우저 `Origin`과 현재 공개 호스트의 HTTPS Origin이 일치하는 경우 |
| 거부 처리 | Origin 누락 또는 불일치 시 JSON 403 반환 및 기존 보안 응답 헤더 유지 |
| 보호 경로 | 세션 기반 파일함 쓰기 라우트 및 포트폴리오 `/admin/save`를 포함한 포털 unsafe 요청 전체 |

## 4. TDD 검증 기록

| 단계 | 추가한 실패 테스트 | RED 확인 | GREEN 확인 |
|---|---|---|---|
| 메모 제한 영속화 | 6회 실패 후 모듈 재적재 상태에서도 429 유지 | 기존 메모리 전용 구현에서 상태 파일 부재 및 재시작 제한 초기화 확인함 | JSON 상태 파일 저장 후 429 유지 확인함 |
| 메모 제한 다중 프로세스 | 4개 독립 프로세스의 실패 기록이 모두 JSON에 남는지 확인 | 기존 구현에 프로세스 공유 상태가 없어 실패함 | 파일 잠금 적용 후 각 서비스 4건 보존 확인함 |
| 파일함 CSRF | 로그인 세션으로 `/files/folders` 교차 Origin POST 거부 및 동일 Origin 폼 성공 | 기존 구현에서 교차 Origin 요청이 처리됨 | 교차 Origin 403, 동일 Origin 303 및 폴더 생성 확인함 |
| 포트폴리오 CSRF | 로그인 세션으로 `/admin/save` 교차 Origin POST 거부 및 동일 Origin 폼 성공 | 기존 구현에서 교차 Origin 요청이 303으로 처리됨 | 교차 Origin 403, 동일 Origin 303 확인함 |

## 5. 검토 결과

| 구분 | 결과 | 비고 |
|---|---|---|
| Important I-1: 포털 Origin 검증 누락 | 해소 | unsafe 공통 미들웨어 및 세션 경로 회귀 테스트 추가함 |
| Important I-2: 메모 인증 실패 제한의 내구성·원자성 누락 | 해소 | 재시작·프로세스 동시성 회귀 테스트를 Book/YouTube 각각 추가함 |
| 서버 기동·스케줄러 변경 | 없음 | 요청 범위에 따라 미변경함 |

## 6. 확인 필요 사항

추가 확인 필요 사항 없음.

## 7. 후속 조치

병합 전 최종 전체 회귀 및 코드 리뷰 수행 필요함.
