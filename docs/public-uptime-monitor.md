# 공개 상태 Telegram 알림

## 목적

GitHub Actions가 외부에서 `https://len.pe.kr/health`를 약 5분 간격으로 확인함. N100, Cloudflare Tunnel 또는 Portal 경로가 중단되어도 GitHub에서 장애와 복구 상태를 Telegram으로 알림.

## 알림 기준

| 상황 | Telegram 메시지 | 중복 처리 |
|---|---|---|
| 공개 건강 점검 실패 | `[개인서버 장애]` | 장애 전환 시 1회 |
| 이후 점검 성공 | `[개인서버 복구]` | 복구 전환 시 1회 |

점검이 실패한 동안에는 같은 장애 메시지를 반복 전송하지 않음. GitHub 저장소에 열린 `[SRE] len.pe.kr 공개 상태 장애` 이슈가 있는지로 상태를 보관하며, 정상 복구 시 해당 이슈를 닫음.

## 최초 설정

GitHub 저장소의 `Settings` → `Secrets and variables` → `Actions`에서 아래 Repository secret 두 개를 추가함.

| Secret 이름 | 값 | 비고 |
|---|---|---|
| `UPTIME_TELEGRAM_BOT_TOKEN` | Telegram Bot token | 기존 SRE 알림 Bot을 사용 가능함 |
| `UPTIME_TELEGRAM_CHAT_ID` | 수신할 개인 또는 그룹 chat ID | 기존 SRE 수신처를 사용 가능함 |

Secret 값은 Git, workflow 출력, GitHub 이슈에 기록하지 않음.

## 수동 점검

GitHub 저장소의 `Actions` → `Public Portal Uptime Monitor` → `Run workflow`를 선택해 즉시 실행 가능함. 정상 상태에서는 Telegram 메시지를 보내지 않음.

## 한계

- GitHub Actions의 예약 실행은 약 5분 간격이며, GitHub 부하에 따라 조금 늦어질 수 있음.
- GitHub Actions 또는 Telegram API 자체 장애는 별도로 감지하지 못함.
