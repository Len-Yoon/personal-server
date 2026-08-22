# Task 4 수행 보고서: 차량관리 Telegram 워커

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 작업명 | Task 4 독립 컨테이너·실행 루프·운영 등록 |
| 작업일 | 2026-08-22 |
| 작업 범위 | 신규 `car-care-worker` Compose 서비스, lifecycle, 테스트 등록, 운영 문서 |
| 제외 범위 | 기존 서비스 명령, crawler/news scheduler, 배포 스크립트, Caddy |
| 성공 기준 | 포트 미노출, `data/car-care`만 상태 볼륨 사용, lifecycle·Compose 테스트 및 관련 검증 통과 |

## 2. 변경 내용

| 구분 | 변경 내용 | 검토 결과 |
|---|---|---|
| 실행 루프 | `python -m app.main` 진입점 추가. SQLite 초기화, Telegram 처리 5초 간격, Hyundai 관측 5분 간격, SIGINT/SIGTERM 종료 처리 구현 | 반영됨 |
| lifecycle | 명령 응답과 Hyundai monitor 알림을 한 번의 `run_once()`로 처리 | 반영됨 |
| Compose | `car-care-worker` 신규 서비스 추가. `restart: unless-stopped`, `.env`, `./data/car-care:/data/car-care` 설정 | 반영됨 |
| 격리 | 공개 `ports` 및 소스 마운트 미설정. N100 override에 read-only, capability drop, no-new-privileges, 자원 한도 적용 | 반영됨 |
| 운영 등록 | 6개 차량관리 테스트 모듈을 `car-care-worker` 서비스 스위트에 등록 | 반영됨 |
| 문서 | 환경변수, 지원 명령, `/정비완료` 수동 초기화, Hyundai 승인 의존성, Caddy 인바운드 경로 없음 문서화 | 반영됨 |

## 3. TDD 증적

| 단계 | 실행 명령 | 결과 |
|---|---|---|
| RED | `PYTHONPATH=car-care-worker python3 -m unittest tests.test_compose_config tests.car_care_worker.test_main -v` | 실패 확인. `car-care-worker` Compose 서비스가 없어 Compose 계약 테스트가 실패했고, `app.main` 부재로 import가 실패함. |
| GREEN(계약) | 동일 명령 재실행 | 10개 테스트 통과. 신규 서비스 격리 계약과 `run_once()`의 명령 응답·monitor 알림 전송 확인됨. |
| GREEN(차량관리) | `PYTHONPATH=car-care-worker python3 -m unittest discover -s tests/car_care_worker -v` | 28개 테스트 통과. |
| GREEN(등록 스위트) | `python3 tests/run_service_tests.py --suite car-care-worker --suite maintenance` | `car-care-worker` 28개, `maintenance` 43개 테스트 통과. |

## 4. 구성 및 범위 검증

| 항목 | 실행 결과 |
|---|---|
| Compose 기본 구성 | `docker compose config --quiet` 통과. 작업 트리에 `.env`가 없어 비밀값 없는 빈 임시 `.env`로 검증 후 즉시 제거함. |
| Compose N100 override | `docker compose -f docker-compose.yml -f docker-compose.n100.yml config --quiet` 통과. |
| 공백·오류 | `git diff --check` 출력 없음. |
| 변경 범위 | 신규 `car-care-worker/`, lifecycle 테스트, Compose, `.env.example`, 테스트 실행기, README, 운영 참조 문서로 한정됨. |
| 금지 영역 | 기존 서비스 command, crawler/news scheduler, 배포 스크립트, Caddy 미변경. |

## 5. 확인 필요 사항

| 항목 | 내용 |
|---|---|
| Hyundai 상용 연동 | 실제 API 사용 승인 및 더 뉴 그랜저 응답 필드 확인 필요. 확인 전 `HYUNDAI_*` 미설정 상태로 수동 모드 운영 필요. |
| Telegram 운영값 | `CAR_CARE_TELEGRAM_BOT_TOKEN`, `CAR_CARE_TELEGRAM_CHAT_ID`는 운영 `.env`에만 설정 필요. 저장소·문서·로그에 실제 값 미포함. |

## 6. 후속 조치

1. 운영 환경 `.env`에 차량관리 Telegram 필수 값을 설정함.
2. 최초 정비 이력을 `/정비완료 엔진오일 [km]`, `/정비완료 미션오일 [km]`로 등록함.
3. Hyundai 연동 승인·응답 필드 검증 후 선택 환경변수를 설정함.

---

## 7. 검토 보완 1차

### 7.1 원인 및 조치

| 검토 항목 | 확인된 원인 | 보완 조치 |
|---|---|---|
| 배포·부트스트랩 누락 | `deploy-n100.sh`, `windows-bootstrap.sh`의 명시 서비스 목록에 `car-care-worker`가 없음 | 두 기존 목록에 신규 워커만 추가함. 기존 서비스 순서·명령과 Caddy 설정은 변경하지 않음. |
| SIGTERM 종료 지연 | Telegram getUpdates가 25초 long polling·30초 요청 timeout을 사용하고, 업데이트 처리 후 종료 상태를 확인하지 않음 | Telegram long polling을 5초, 요청 timeout을 10초로 제한함. 업데이트 처리 뒤 stop event를 확인하고 차량 관측을 생략함. Compose `stop_grace_period: 15s`로 요청 timeout보다 긴 종료 유예를 설정함. |
| 빈 DB 경로 | 빈 `CAR_CARE_DB_PATH`가 `Path(\"\")`로 해석됨 | 공백을 제거한 값이 비어 있으면 `/data/car-care/car-care.sqlite3` 기본 경로를 사용하도록 수정함. |
| Compose 테스트 범위 | 워커 뒤의 Compose 전체를 검사해 서비스 범위가 정확히 분리되지 않음 | 서비스 블록을 분리해 public port 없음, 정확히 하나의 `./data/car-care:/data/car-care` 볼륨, N100 `read_only: true`를 확인하도록 보완함. |

### 7.2 TDD 및 검증 증적

| 단계 | 실행 명령 | 결과 |
|---|---|---|
| RED | `PYTHONPATH=car-care-worker python3 -m unittest tests.car_care_worker.test_main tests.car_care_worker.test_telegram tests.test_compose_config tests.test_deploy_n100 tests.test_windows_bootstrap -v` | 37개 실행 중 1개 import 오류와 4개 실패 확인. `_database_path` 부재, Telegram timeout 25/30초, `stop_grace_period` 부재, deploy/bootstrap 서비스 목록 누락이 원인으로 확인됨. |
| GREEN | 동일 명령 재실행 | 39개 테스트 통과. stop event 처리, 빈 DB 경로 기본값, 요청 경계 timeout, Compose 격리, deploy/bootstrap 등록 확인됨. |

### 7.3 보완 범위

| 구분 | 변경 여부 |
|---|---|
| `car-care-worker` lifecycle·Telegram client | 변경함 |
| Compose 신규 워커 설정 | 변경함 |
| `scripts/deploy-n100.sh`, `scripts/windows-bootstrap.sh` 신규 워커 등록 | 변경함 |
| 기존 서비스 command, crawler/news scheduler, Caddy | 변경하지 않음 |
| 비밀값 | 생성·기록하지 않음 |

---

## 8. 최종 검토 보완

### 8.1 중요 지적 조치 결과

| 검토 항목 | 조치 내용 | 검증 결과 |
|---|---|---|
| 전송 실패 시 alert suppression | `VehicleMonitor.observe()`는 알림 후보만 생성하고, Telegram `send()` 성공 뒤에만 `acknowledge()`로 warning·maintenance·trip 상태를 저장하도록 변경함 | 전송 실패 뒤 다음 관측에서 동일 경고가 재생성되고, 성공 ACK 뒤에만 억제됨을 확인함 |
| 수동 모드 정비 결과 | `/주행거리 <km>`가 엔진오일·미션오일 정비 규칙을 즉시 평가하여 명령 응답에 포함하도록 변경함. `/차량`에는 다음 정비 상태를 표시하도록 변경함 | 두 정비 항목의 사전 알림 및 다음 정비 상태 표시 테스트 통과 |
| Hyundai 비활성·오류 구분 | `HyundaiFetchResult`로 `disabled`, `success`, `error` 상태를 구분함. 요청 오류와 JSON 파싱 오류를 별도 분류하고, 실제 오류 알림은 성공 전송 후에만 날짜 상태를 저장해 하루 1회로 제한함 | 자격 증명 미설정은 외부 호출 없이 disabled, 요청·파싱 오류는 error, 같은 날 오류 알림 1회 전송 확인 |
| Compose 환경변수 최소화 | `car-care-worker`의 `env_file: .env`를 제거하고 Telegram·DB 및 Hyundai 필요 변수 8개만 명시 주입함 | service scope 테스트로 env_file 부재와 정확한 환경변수 목록 확인 |

### 8.2 TDD 증적

| 단계 | 실행 명령 | 결과 |
|---|---|---|
| RED | `PYTHONPATH=car-care-worker python3 -m unittest tests.car_care_worker.test_main tests.car_care_worker.test_telegram tests.car_care_worker.test_hyundai tests.car_care_worker.test_vehicle_monitor tests.test_compose_config -v` | 33개 실행 중 3개 실패·8개 오류 확인. suppression ACK 부재, 수동 정비 결과·다음 정비 상태 부재, Hyundai 결과 객체 부재, broad env_file 사용이 원인으로 확인됨. |
| RED(파싱 구분) | `PYTHONPATH=car-care-worker python3 -m unittest tests.car_care_worker.test_hyundai -v` | JSON 파싱 오류가 request로 분류되는 실패 확인. |
| GREEN | `PYTHONPATH=car-care-worker python3 -m unittest discover -s tests/car_care_worker -v` | 차량관리 36개 테스트 통과. |
| GREEN(등록 스위트) | `python3 tests/run_service_tests.py --suite car-care-worker --suite maintenance` | car-care-worker 36개, maintenance 44개 테스트 통과. |

### 8.3 범위 확인

| 항목 | 결과 |
|---|---|
| 변경 영역 | `car-care-worker`, 신규 워커 Compose 환경변수, 관련 차량관리·Compose 테스트, Task 4 보고서로 한정됨 |
| 미변경 영역 | crawler/news, 기존 서비스 command, Caddy, 배포·부트스트랩 스크립트 미변경 |
| 비밀값 | 실제 Telegram·Hyundai 값 미생성·미기록 |
