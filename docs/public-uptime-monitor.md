# 공개 상태 Telegram 알림

## 목적

N100의 로컬 감시기와 GitHub Actions가 상호 보완적으로 장애·복구 전환을 감시함. N100 감시기는 Cloudflare Tunnel의 로컬 상태를 복구하면서 직접 알리고, GitHub Actions는 `https://len.pe.kr/health`를 N100과 독립된 외부 경로에서 확인해 알림을 보냄.

## 알림 기준

| 상황 | Telegram 메시지 | 중복 처리 |
|---|---|---|
| 공개 건강 점검 실패 | `[개인서버 장애] 외부 점검에서 접속 실패를 확인했습니다. 자동복구 상태를 확인하세요.` | 장애 전환 시 1회 |
| 장애 알림 전송이 성공한 뒤 이후 점검 성공 | `[개인서버 복구] 외부 점검에서 정상 응답을 다시 확인했습니다.` | 복구 전환 시 1회 |

점검이 실패한 동안에는 같은 장애 메시지를 반복 전송하지 않음. GitHub 저장소에 열린 `[SRE] len.pe.kr 공개 상태 장애` 이슈와, Telegram 장애 메시지 전송이 성공했음을 표시하는 기록으로 상태를 보관하며 정상 복구 시 해당 이슈를 닫음.

Telegram Secret이 누락되었거나 Telegram 장애 메시지 전송이 실패하면 GitHub Actions는 외부 health 점검과 장애 Issue 상태 처리를 계속함. 전송이 가능한 다음 실패 점검에서 장애 메시지 전송을 다시 시도함. 성공 기록이 남기 전까지 복구 전환 메시지를 보내지 않음. 알림 전송 증적이 없는 Issue가 정상 전환을 맞으면 복구 Telegram을 보내지 않고 `알림 미전송` 증적을 남긴 뒤 종료함. 따라서 장애 알림이 수신됐는지와 이후 정상 점검이 모두 확인되어야 복구 알림이 발송됨.

### N100 직접 Tunnel 알림

N100 감시기는 `cloudflared-personal-server.service`의 상태와 Tunnel 프로세스를 주기적으로 확인함. Tunnel 장애를 처음 확인하면 `[개인서버 장애]` 메시지를 1회 전송하고, 기존 임계치에 따라 로컬 복구를 시도함. 이후 Tunnel 서비스와 프로세스가 정상으로 돌아오면 `[개인서버 복구]` 메시지를 1회 전송함.

Windows 예약 작업은 Supervisor를 하나만 실행하며, Supervisor는 초기 120초 대기 뒤 3분 주기 Daemon을 자식으로 관리함. Daemon이 비정상 종료되면 15초 뒤 재기동하고, Supervisor·Daemon 잠금으로 중복 실행을 막음. N100 Tunnel은 로컬 NodePort·서비스·프로세스·공개 health가 모두 정상일 때만 정상으로 판정하며, NodePort 장애는 Tunnel 알림·재기동 대상에서 제외함. 이 구조는 Tunnel 감시가 Daemon 종료나 active-but-disconnected 상태에서 멈추지 않게 하는 보완 경로이며, N100 전원·네트워크·WSL 또는 Windows 작업 스케줄러 자체가 동작하지 않는 경우는 복구·직접 알림 범위 밖임.

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

N100 감시기는 장애 감지·복구 요청·조치 실행 결과·Tunnel 알림 전환만 `C:\personal-server\data\recovery-events.jsonl`에 기록함. 정상 주기 점검은 기록하지 않으며 최근 200건만 유지함. 조치 실행 결과가 `accepted`여도 실제 정상 복구는 다음 점검의 `health_restored` 이벤트로만 확인함. 각 행에는 UTC 저장 시각, 구성요소, 이벤트, 결과, 수행 동작만 포함하고 자격증명·명령 인수·Telegram 응답은 기록하지 않음.

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

GitHub 저장소의 `Actions` → `Public Portal Uptime Monitor` → `Run workflow`를 선택해 즉시 실행 가능함. 정상 상태에서는 Telegram 메시지를 보내지 않음.

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
- GitHub Actions 또는 Telegram API 자체 장애는 별도로 감지하지 못함. Telegram 전송 실패 여부는 해당 workflow 실행 로그에서 확인 필요함.
- N100 직접 알림은 N100 전원·WSL·네트워크 또는 Credential Manager 접근이 불가능한 경우 전송되지 않음. 이 경우 GitHub Actions 외부 점검 경로가 보완함.
- GitHub Actions와 N100은 감지 주기가 다르므로 장애·복구 메시지가 중복 수신될 수 있음. 각 경로는 자체 상태 전환 기준으로 중복을 억제함.
