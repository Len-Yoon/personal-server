# 로컬 장애 진단·복구 실습

## 1. 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 작성일 | 2026-10-17 | Asia/Seoul |
| 목적 | 실패 관측 → 원인 증거 → 해제 → 복구·보존 확인을 연습함 | 알려진 장애의 자동 훈련 |
| 실행 환경 | 저장소의 Book Python 환경 | 클라우드·새 VM·N100 변경 없음 |
| 기준 자료 | [설계](superpowers/specs/20261017_장애진단복구실습_설계서.md), [검증 결과](reviews/20261017_장애진단복구실습_검증결과.md) | 실제 제품 함수 사용 |

## 2. 핵심 요약

임시 로컬 fixture에서 검색 의존성 지연, SQLite 쓰기 잠금, SQLite 페이지 제한을 실제로 발생시키고 복구함. 제품 소스와 운영 데이터는 수정하지 않음. 출력에는 단계별 시각·경과 시간, 실패 원자성, 동일 요청 재전송, 프로세스 회수·fixture 삭제 결과가 포함됨.

## 3. 상세 내용

### 3.1 실행

저장소 루트에서 기존 결과가 없는 출력 경로로 실행함. 신규 환경은 `tests/ci_test_matrix.json`의 Book 의존성 설치 절차를 따름.

```bash
.venv/book-memo/bin/python book-memo/tools/incident_recovery_lab.py --output /tmp/incident-first.json
.venv/book-memo/bin/python book-memo/tools/incident_recovery_lab.py --scenario sqlite_busy --output /tmp/incident-busy.json
.venv/book-memo/bin/python book-memo/tools/incident_recovery_lab.py --repeats 3 --output /tmp/incident-repeat.json
python3 -m json.tool /tmp/incident-first.json
```

기본 3개 시나리오 각 1회, 최대 각 3회임. `--deadline`은 전체 실행 제한으로 기본·최대 180초이며 0초 초과임. 단일 명령 응답은 최대 12초, 정리 유예·강제 회수는 추가 최대 10초임. 기존 파일·symlink 출력은 거부함. 외부 URL·DB 경로·SQL·PID 입력 옵션은 제공하지 않음.

### 3.2 진단·복구 Runbook

| 단계 | 확인할 증거·행동 | 성공 기준 | 비고 |
|---|---|---|---|
| 기준 확인 | baseline 관측과 Git SHA·dirty·source_hashes 확인 | 정상 검색 3개 또는 초기 DB snapshot 존재 | dirty 실행은 고정 소스 증거로 사용하지 않음 |
| 실패 확인 | failure.error_code 또는 statuses 확인 | books만 unavailable, BUSY=5, FULL=13 | 다른 오류를 목표 장애 성공으로 세지 않음 |
| 원인 설명 | capacity와 baseline page_count 대조, 소유 locker와 오류 코드 관계 설명 | 주입한 제한과 실제 오류를 연결함 | 자동 판정은 독립적인 사람의 RCA 평가가 아님 |
| 조치 설명 | 검색 지연 해제 / locker rollback·종료 / 페이지 한도 확대를 구분함 | recovery_action_completed 존재 | 도구가 소유한 fixture에만 적용 |
| 복구 확인 | 정상 검색 또는 data_preserved·exactly_once·integrity_ok 확인 | 각 값 true, 메모 version=2 | 같은 요청 ID 2회에도 수정 1회 |
| 정리 확인 | cleanup_ok·fixture_removed·children_exited_cleanly 확인 | 모두 true, cleanup_verified 존재 | 종료 실패는 유효 실습으로 세지 않음 |
| 중단 분석 | valid=false, reason, remaining_cases와 누락 이벤트 확인 | 부분 결과를 보존하고 미완료를 구분함 | SIGINT/SIGTERM으로 중단 가능 |

수동 학습에서는 실행 전 예상 증상·원인·확인 순서·최소 복구 조치를 적고, 결과 JSON과 대조하여 잘못된 가설을 수정함. 이 CLI는 자동으로 장애와 해제를 수행하므로 사람이 조치할 때까지 대기하는 대화형 훈련은 아님. 운영 DB 잠금 해제·호스트 정리 명령으로 확장하지 않음.

### 3.3 시간과 안전 경계

| 결과 | 해석 | 비고 |
|---|---|---|
| detection_seconds | 주입 ack부터 실패 응답을 부모가 관측한 시간 | 제품 기본 SQLite busy timeout 포함 |
| automatic_cause_check_seconds | 실패 관측부터 후속 증거 검증 완료 | DB snapshot 확인을 별도 명령으로 수행 |
| recovery_check_seconds | 해제 시작부터 복구 검증 완료 | 정리 시간 제외 |
| SQLite FULL | 현재 페이지 수로 제한한 DB의 실제 SQLITE_FULL | OS ENOSPC·호스트 디스크 포화 아님 |
| 데이터 보존 | 고정 fixture의 논리 snapshot·버전·수정 이력·요청 원장 검증 | 서비스 전체 RPO=0을 증명하지 않음 |

새 임시 디렉터리와 loopback socket만 사용함. 자식은 환경을 제한한 뒤 제품 모듈을 불러옴. DB fixture 파일 합계는 최대 4MiB, 페이지 제한은 최대 1MiB 범위에서만 허용함. 자신이 시작한 자식만 종료·회수하며 살아 있는 자식이 있으면 fixture를 삭제하지 않음. 결과에 환경 전체·오류 원문·개인 경로·메모 본문을 기록하지 않음.

## 4. 검토 결과

제품 `global_search.search_all`과 `book_service.update_memo`를 실제 호출함. SQLite 구조화 오류 코드, 실패 후 전체 논리 snapshot, 동일 요청 재전송과 integrity_check를 검증함. [측정 원본·회귀·독립 검토](reviews/20261017_장애진단복구실습_검증결과.md)를 함께 확인함.

## 5. 확인 필요 사항

앱 HTTP 503·화면 오류 처리, 운영 경보·알림 전달, 실제 사용자 영향, 사람의 진단 시간, 운영 MTTD/MTTR·RTO/RPO는 미검증임. OS 파일시스템 ENOSPC와 실제 운영 DB 복원 훈련은 이번 범위에 포함하지 않음.

## 6. 후속 조치

후속 단계는 배포 표준화임. 이번 실습 결과를 운영 용량·복구 목표로 그대로 전환하지 않음.
