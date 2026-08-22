# Task 3 Telegram·Hyundai 어댑터 작업 보고서

| 항목 | 내용 |
|---|---|
| 작업 범위 | Telegram long polling·전송, 명령 처리, Hyundai API opt-in 어댑터 구현 |
| 제외 범위 | `main.py`, Docker Compose, 서버 기동, 스케줄러, 뉴스 크롤러 |
| 기준 자료 | `.superpowers/sdd/2026-08-22-car-care-telegram/task-3-brief.md`, Task 1·2 서비스 인터페이스 |
| 작업 일자 | 2026-08-22 |

## 1. 핵심 요약

- Telegram `getUpdates` 25초 long polling 및 `sendMessage` 8초 전송 경계를 구현함.
- 허용 chat ID 외 메시지는 처리하지 않으며, `/차량`, `/주행거리`, `/정비완료`, `/정비목록`, `/알림테스트` 명령을 구현함.
- 사용자 표기 항목 `엔진오일`, `미션오일`을 내부 항목 `engine_oil`, `transmission_oil`로 변환함.
- Hyundai 필수 환경변수 미설정 시 네트워크 요청 없이 `None`을 반환하는 no-op 모드를 구현함.
- 토큰과 HTTP 헤더를 로그에 기록하는 코드를 추가하지 않음.

## 2. RED 검증 결과

실행 명령:

```bash
PYTHONPATH=car-care-worker python3 -m unittest tests.car_care_worker.test_telegram tests.car_care_worker.test_hyundai -v
```

| 검증 항목 | 결과 | 근거 |
|---|---|---|
| Telegram 어댑터 테스트 | 실패 | `app.services.telegram` 모듈 미존재로 ImportError 발생 |
| Hyundai 어댑터 테스트 | 실패 | `app.services.hyundai` 모듈 미존재로 ImportError 발생 |
| 실패 원인 적합성 | 적합 | 테스트 대상 기능이 아직 구현되지 않은 상태를 확인함 |

## 3. 구현 결과

| 파일 | 구현 내용 |
|---|---|
| `car-care-worker/app/services/telegram.py` | Telegram 업데이트 수신·전송, chat ID 제한, 차량관리 명령 처리 구현 |
| `car-care-worker/app/services/hyundai.py` | 자격 증명 미설정 no-op, odometer·DTE·지원 경고 5종만 매핑 구현 |
| `tests/car_care_worker/test_telegram.py` | 권한 제한, 명령 별 상태 변경, long polling, 전송 timeout 검증 추가 |
| `tests/car_care_worker/test_hyundai.py` | 자격 증명 미설정 시 외부 요청 없이 `None` 반환 검증 추가 |

## 4. GREEN 및 통합 검증 결과

| 실행 명령 | 결과 |
|---|---|
| `PYTHONPATH=car-care-worker python3 -m unittest tests.car_care_worker.test_telegram tests.car_care_worker.test_hyundai -v` | 10건 통과 |
| `PYTHONPATH=car-care-worker python3 -m unittest discover -s tests/car_care_worker -v` | 26건 통과 |
| `git diff --check` | 통과 |

## 5. 확인 필요 사항

- Hyundai Developers 상용 API 승인 후 실제 인증 방식, endpoint 및 응답 필드명이 확정되지 않음.
- 실제 API 응답 확인 전에는 설정 누락 시 no-op 동작만 검증됨.

## 6. 후속 조치

- Task 4에서 실행 루프가 `TelegramClient`와 `HyundaiClient`를 연결하고 polling offset·주기 관리를 구현 필요.
