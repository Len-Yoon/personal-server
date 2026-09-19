# 공개 상태 Telegram 알림

## 목적

N100의 로컬 감시기와 GitHub Actions가 상호 보완적으로 장애·복구 전환을 감시함. N100 감시기는 Cloudflare Tunnel의 로컬 상태를 복구하면서 직접 알리고, GitHub Actions는 네 공개 서비스 health를 N100과 독립된 외부 경로에서 확인해 알림을 보냄.

| 식별자 | 공개 health URL |
|---|---|
| `portal` | `https://len.pe.kr/health` |
| `news` | `https://news.len.pe.kr/health` |
| `youtube_memo` | `https://memo.len.pe.kr/health` |
| `book_memo` | `https://books.len.pe.kr/health` |

## 알림 기준

| 상황 | Telegram 메시지 | 중복 처리 |
|---|---|---|
| 공개 health 중 하나 이상 실패 | `[외부 장애]`와 함께 문제·영향·실패 서비스의 사용자용 이름을 표시함 | 장애 전환 시 1회 |
| 장애 알림 전송이 성공한 뒤 네 공개 health 모두 성공 | `[외부 복구]`와 함께 장애 당시 서비스의 사용자용 이름을 표시함 | 복구 전환 시 1회 |

외부 상태 알림은 `portal`, `news`, `youtube_memo`, `book_memo` 식별자를 각각 Portal, News Hub, YouTube Memo, Book Memo의 서비스별 사용자용 이름으로 변환함. Telegram에는 내부 IP·Pod 이름·Compose 컨테이너 이름을 표시하지 않음. 점검 중 하나라도 실패한 동안에는 같은 장애 메시지를 반복 전송하지 않음. 장애가 이어지는 동안 다른 서비스도 실패하면 대상 목록에 합쳐 보존하며, 네 서비스가 모두 성공할 때만 정상 복구로 판정함. GitHub 저장소에 열린 `[SRE] len.pe.kr 공개 상태 장애` 이슈와 Telegram 전송 성공 기록으로 상태를 보관하며 정상 복구 시 해당 이슈를 닫음. 이슈에는 실패한 고정 서비스 식별자만 기록하며 URL query, 응답 본문, Telegram 자격증명은 기록하지 않음.

Telegram Secret이 누락되었거나 Telegram 장애 메시지 전송이 실패하면 GitHub Actions는 외부 health 점검과 장애 Issue 상태 처리를 계속함. 전송이 가능한 다음 실패 점검에서 장애 메시지 전송을 다시 시도함. 성공 기록이 남기 전까지 복구 전환 메시지를 보내지 않음. 알림 전송 증적이 없는 Issue가 정상 전환을 맞으면 복구 Telegram을 보내지 않고 `알림 미전송` 증적을 남긴 뒤 종료함. 따라서 장애 알림이 수신됐는지와 이후 정상 점검이 모두 확인되어야 복구 알림이 발송됨.

### 감시 경로 실패 알림

공개 health 결과와 별도로, `Public Uptime Monitor Path Observer`가 완료된 공개 감시 실행을 읽어 감시 경로의 실패를 구분함. 이 관찰자는 원본 workflow의 고정된 job·Telegram step 결론만 조회하며, 원본 코드·artifact·실행 로그는 내려받거나 실행하지 않음.

| 구분 | Telegram 메시지 | GitHub Issue 처리 | 다음 정상 전환 |
|---|---|---|---|
| 감시 실행 실패 | `[감시 실행 실패]` | `[SRE] 공개 감시 경로 장애` Issue를 열거나 유지함 | 다음 성공적으로 끝난 점검에서 `[감시 경로 복구]`를 1회 전송하고 Issue를 닫음 |
| Telegram 전달 실패 | `[감시 알림 전송 실패]`를 전송할 수 있을 때만 보냄 | 전달에 성공하기 전까지 성공 marker 없이 Issue를 유지함 | 다음 성공적으로 끝난 점검에서 이전 전달 성공 기록이 있을 때만 복구를 알림 |
| 취소·건너뜀·대기시간 초과 | 알리지 않음 | Issue를 변경하지 않음 | 해당 없음 |

관찰 workflow 자체의 Telegram 전송이 실패하면 Telegram으로 즉시 알릴 수 없으므로 Issue 증적만 유지함. 다음 원본 공개 감시 실행에서 Telegram 전송을 다시 시도함. Telegram Bot token, Chat ID, API 응답 본문, 원본 실행 오류 원문은 Telegram·Issue·workflow 출력에 기록하지 않음.

GitHub Actions의 예약 실행 자체가 시작되지 않는 경우는 이 workflow로 확인할 실행 결과가 없으므로, 새 외부 제공자 없이 감지할 수 없음. 수동 확인이 필요하면 GitHub Actions 실행 이력과 열린 `[SRE] 공개 감시 경로 장애` Issue를 확인함.

### K3s 뉴스 수집 지연 알림

`NewsCollectionStale`는 뉴스 수집 성공 시각·시도 시각·연속 실패 수를 기준으로 Alertmanager가 감지함. firing Telegram에는 `다음 수집 상태를 확인 중입니다.`라고 표시함. 현재 이 알림은 수집 상태를 감지·알릴 뿐 crawler의 자동 재시작 또는 자동 복구를 수행하지 않음. 수신 시 Prometheus target과 crawler 로그·스케줄러 상태를 확인 필요함.

### N100 직접 Tunnel 알림

N100 감시기는 `cloudflared-personal-server.service`의 상태와 Tunnel 프로세스를 주기적으로 확인함. Tunnel 장애를 처음 확인하면 `[개인서버 장애]` 메시지를 1회 전송하고, 기존 임계치에 따라 로컬 복구를 시도함. 이후 Tunnel 서비스와 프로세스가 정상으로 돌아오면 `[개인서버 복구]` 메시지를 1회 전송함.

Windows 예약 작업은 `PersonalServer-WSL-KeepAlive`와 Supervisor를 각각 `BootTrigger`와 `Password` LogonType으로 등록해 로그인 없이 부팅 시 실행함. `-InstallTask` 등록 때 Task Scheduler 암호는 운영자가 N100 콘솔에서 직접 입력하며, 암호는 Git·문서·로그·명령 출력·환경 변수에 평문으로 저장하거나 출력하지 않음. Supervisor는 초기 120초 대기 뒤 3분 주기 Daemon을 자식으로 관리함. Daemon이 비정상 종료되면 15초 뒤 재기동하고, Supervisor·Daemon 잠금으로 중복 실행을 막음. N100 Tunnel은 로컬 NodePort·서비스·프로세스·공개 health가 모두 정상일 때만 정상으로 판정하며, NodePort 장애는 Tunnel 알림·재기동 대상에서 제외함. 이 구조는 Tunnel 감시가 Daemon 종료나 active-but-disconnected 상태에서 멈추지 않게 하는 보완 경로이며, N100 전원·네트워크·WSL 또는 Windows 작업 스케줄러 자체가 동작하지 않는 경우는 복구·직접 알림 범위 밖임.

### 재시작 뒤 초기 점검

Supervisor가 잠금을 획득하고 시작되면 `supervisor_started` 이벤트를 기록함. `BootTrigger`에 의한 시작, 예약 작업 재시작, 수동 `-Supervisor` 실행은 모두 같은 이벤트를 기록하므로 Windows 부팅의 증명으로 사용하지 않음. WSL·K3s·Portal의 정상 여부는 별도 `post_boot_check` 결과로 확인함. Supervisor는 초기 대기 후 `post_boot_check`를 정확히 1회 수행함. 초기 점검은 WSL, K3s, Portal 상태를 확인하고, Tunnel이 정상인 경우 `https://len.pe.kr/health`를 10초 간격으로 3회 호출해 모두 HTTP 200인지 확인함. 시작·완료·실패 결과는 `recovery-events.jsonl`에 이벤트로만 기록하며, 초기 점검 자체에 대한 신규 Telegram 메시지는 발송하지 않음. Telegram은 기존 정책대로 Cloudflare Tunnel 장애·복구 전환에만 사용함.

`post_boot_check`와 종전 `boot_observed` 구현의 N100 적용과 검증을 완료함. 2026-09-18 15:49에 재부팅 뒤 종전 `boot_observed` 이벤트가 기록된 것을 확인했으며, 외부 health는 10초 간격 3회 모두 HTTP 200으로 확인함. 현재 소스의 `supervisor_started` 이벤트는 별도 N100 적용 승인 뒤에만 실제 기록 여부를 확인함. 초기 점검 결과는 `recovery-events.jsonl`의 `post_boot_check` 이벤트로 확인하며, 다음 유지보수 재부팅에서도 KeepAlive 실행 상태와 함께 동일 기준으로 점검함.

N100 알림 자격증명은 `window` 사용자 계정의 Windows Credential Manager에서 `personal-server-tunnel-telegram` 대상을 읽음. 일반 자격 증명의 사용자 이름에는 Telegram Chat ID를, 암호에는 Bot token을 입력함. 실제 값은 명령 출력·문서·로그에 표시하지 않음.

| 항목 | 기준 |
|---|---|
| 자격증명 저장소 | Windows Credential Manager |
| 대상명 | `personal-server-tunnel-telegram` |
| 값 형식 | 사용자 이름=Telegram Chat ID, 암호=Bot token |
| 금지 위치 | Git, 평문 설정 파일, 환경 변수, `recovery-state.json`, 로그 |
| 장애 알림 | Tunnel 장애 최초 감지 시 1회, 전송 실패 시 다음 점검에서 재시도 |
| 복구 알림 | 장애 알림 전송이 확정된 뒤 Tunnel 정상 전환 시 1회 |

장애 알림 전송 확정 여부만 상태 파일에 보관하며, 자격증명 원문·Telegram API 응답·메시지 식별자는 기록하지 않음. 정상 점검마다 알림을 반복하지 않으며, 감시기와 GitHub Actions는 서로 독립적으로 전환을 판단하므로 동일 장애에 두 경로의 메시지가 수신될 수 있음.

### N100 자동복구 이력 확인

현재 N100 배포 감시기는 장애 감지·복구 요청·조치 실행 결과·Tunnel 알림 전환, 종전 `boot_observed` 및 `post_boot_check` 결과를 `C:\personal-server\data\recovery-events.jsonl`에 기록함. 현재 소스를 N100에 별도 승인으로 적용한 뒤에는 `supervisor_started`가 종전 `boot_observed`를 대체함. 정상 주기 점검은 기록하지 않으며 최근 200건만 유지함. 두 이벤트 모두 기존 UTC `timestamp`를 사용하며, 이벤트 기록은 Telegram 알림을 발생시키지 않음. 조치 실행 결과가 `accepted`여도 실제 정상 복구는 다음 점검의 `health_restored` 이벤트로만 확인함. 각 행에는 UTC 저장 시각, 구성요소, 이벤트, 결과, 수행 동작만 포함하고 자격증명·명령 인수·Telegram 응답은 기록하지 않음.

Windows PowerShell에서 최근 이력을 화면 확인용 시각으로 보려면 다음 읽기 전용 명령을 사용함.

```powershell
Get-Content C:\personal-server\data\recovery-events.jsonl |
  ForEach-Object {
    $event = $_ | ConvertFrom-Json
    [pscustomobject]@{
      시각 = ([DateTimeOffset]::Parse($event.timestamp)).ToOffset([TimeSpan]::FromHours(9)).ToString('yyyy-MM-dd HH:mm')
      구성요소 = $event.component
      이벤트 = $event.event
      결과 = $event.status
      동작 = $event.action
    }
  } | Format-Table -AutoSize
```

## 최초 설정

GitHub 저장소의 `Settings` → `Secrets and variables` → `Actions`에서 아래 Repository secret 두 개를 추가함.

| Secret 이름 | 값 | 비고 |
|---|---|---|
| `UPTIME_TELEGRAM_BOT_TOKEN` | Telegram Bot token | 기존 SRE 알림 Bot을 사용 가능함 |
| `UPTIME_TELEGRAM_CHAT_ID` | 수신할 개인 또는 그룹 chat ID | 기존 SRE 수신처를 사용 가능함 |

Secret 값은 Git, workflow 출력, GitHub 이슈에 기록하지 않음.

N100 직접 알림 자격 증명은 **일반 자격 증명**으로 등록함. 대상명은 정확히 `personal-server-tunnel-telegram`으로 입력하고, 사용자 이름에는 Telegram Chat ID, 암호에는 Bot token을 입력함. Windows 자격 증명이나 도메인 자격 증명이 아닌 일반 자격 증명을 사용해야 함. 등록·확인 과정에서 Bot token과 Chat ID를 명령 출력, 문서, 로그 또는 채팅에 노출하지 않음.

## 수동 점검

GitHub 저장소의 `Actions` → `Public Portal Uptime Monitor` → `Run workflow`를 선택해 즉시 실행 가능함. 네 공개 health가 모두 정상인 상태에서는 Telegram 메시지를 보내지 않음.

N100 직접 알림은 다음 순서로 수동 검증함.

1. 외부 health가 정상인지 먼저 확인함.
2. `window` 사용자로 `cloudflared-personal-server.service`만 중지함.
3. 즉시 서비스가 `inactive`이고 외부 health가 실패하는지 확인함.
4. 감시 주기와 장애 임계치가 경과할 때까지 수동 재시작하지 않음.
5. Tunnel 서비스가 자동으로 다시 `active`가 되고 외부 health가 HTTP 200으로 복구되는지 확인함.
6. Telegram에서 장애 1회와 복구 1회가 수신되는지 확인함.
7. 상태 파일에 자격증명 원문이 없고, 검증 종료 후 외부 health가 정상인지 확인함.

Credential Manager 등록 여부는 자격증명 원문을 출력하지 않고 대상의 존재 여부, 감시기의 설정 상태와 실제 전송 결과로만 확인함. 검증 실패 시 서비스는 수동으로 복구하되, Bot token이나 Chat ID를 채팅·로그·이슈에 붙여 넣지 않음.

## 동작 범위와 한계

- GitHub Actions의 예약 실행은 약 5분 간격이며, GitHub 부하에 따라 지연되거나 실행 간격이 길어질 수 있음. 즉시 확인이 필요하면 수동 점검을 실행함.
- 재부팅이 점검 사이에 끝나면 장애 전환이 확인되지 않으므로 알림을 보내지 않음.
- 이는 재부팅 알림 기능이 아니라, 외부에서 실제 접속 실패를 확인했을 때만 알리는 기능임.
- 완료된 GitHub Actions 실행의 실패와 Telegram 전달 실패는 감시 경로 Issue·Telegram 전환으로 구분함. 다만 GitHub Actions 예약 실행 자체가 시작되지 않는 경우는 외부 제공자 없이 감지하지 못함.
- N100 직접 알림은 N100 전원·WSL·네트워크 또는 Credential Manager 접근이 불가능한 경우 전송되지 않음. 이 경우 GitHub Actions 외부 점검 경로가 보완함.
- GitHub Actions와 N100은 감지 주기가 다르므로 장애·복구 메시지가 중복 수신될 수 있음. 각 경로는 자체 상태 전환 기준으로 중복을 억제함.
