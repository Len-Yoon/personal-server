# N100 Windows·WSL2 운영 환경

## 현재 구성

| 항목 | 기준 |
|---|---|
| 호스트 | Windows N100, Ubuntu-24.04 WSL2 |
| 저장소 | `C:\personal-server` / WSL `/mnt/c/personal-server` |
| Portal | K3s `portal-web` + PVC |
| 나머지 서비스 | Docker Compose |
| 공개 경로 | Cloudflare Tunnel → Caddy → K3s 또는 Compose |
| 모니터링 | K3s Prometheus·Grafana·Telegram SRE relay |

## 재부팅 뒤 자동 시작

`PersonalServer-WSL-KeepAlive`와 `personal-server-autostart`는 `BootTrigger`와 `Password` LogonType으로 등록함. 따라서 Windows 로그인 없이 부팅 시 KeepAlive가 WSL을 유지하고, WSL이 유지되면 사용자 `cloudflared-personal-server.service`, K3s, Docker Compose가 실행 상태를 유지함.

`windows-bootstrap.ps1 -InstallTask` 실행 시 두 작업의 Task Scheduler 암호는 운영자가 N100 콘솔에서 직접 입력함. `window` 계정 암호는 Git·문서·로그·명령 출력·환경 변수에 평문으로 저장하거나 출력하지 않음.

Windows PowerShell에서 확인함.

```powershell
Get-ScheduledTask -TaskName PersonalServer-WSL-KeepAlive
wsl -l -v
```

정상 기준은 작업 상태가 `Running`, `Ubuntu-24.04` 상태가 `Running`임.

## 제한형 자동복구

`personal-server-autostart` 작업은 Windows 시작 시 `-Supervisor`를 실행함. Supervisor는 중복 실행을 잠금으로 막고 초기 120초 안정화 대기 후 `-Daemon` 자식을 하나만 시작함. Daemon은 3분 간격으로 상태를 점검함. Daemon이 비정상 종료되면 Supervisor가 15초 뒤 새 Daemon을 시작하며, 60초 안에 3회 연속 종료되면 60초 backoff 뒤 다시 시도함. Daemon도 별도 잠금으로 중복·레거시 직접 실행을 막음. 2회 연속 health 실패 시 구성요소별 승인된 복구를 시도하고, 핵심 복구가 3회 연속 실패한 경우에만 긴급 재부팅 승격을 검토함. 긴급 재부팅은 Tunnel·Portal·NodePort 단독 장애에 사용하지 않음.

예약 작업은 무기한 실행(`ExecutionTimeLimit PT0S`)되며, Supervisor 자체가 비정상 종료하면 1분 간격으로 최대 3회 재시작함. 예약 작업의 실행 인자는 `windows-bootstrap.ps1 -Supervisor`여야 함. KeepAlive와 Supervisor는 `BootTrigger`와 `Password` LogonType으로 등록해 로그인 전에도 실행하며, Supervisor·Daemon 단일 실행 구조와 재시작 정책을 적용함.

| 대상 | 자동복구 범위 |
|---|---|
| WSL 유지 작업 | 중지된 `PersonalServer-WSL-KeepAlive` 작업 시작 |
| K3s | 실행 중이 아닐 때만 시작. 실행 중인 K3s 재시작 금지 |
| Portal | K3s가 정상이고 Portal이 비가용일 때 제한형 rollout 재시작 |
| NodePort | 상태 확인만 수행. 자동 재시작 금지 |
| Cloudflare Tunnel | 사용자 서비스 시작·재시작 허용. 호스트 긴급 재부팅은 금지 |

Tunnel은 로컬 NodePort·`cloudflared` 서비스·Tunnel 프로세스·공개 `https://len.pe.kr/health`가 모두 정상일 때만 정상으로 판정함. NodePort가 비정상이면 Tunnel 상태는 보류하며 Tunnel Telegram 알림·재기동을 하지 않음. 반대로 로컬 상태는 정상인데 공개 health만 실패하면 2회 연속 실패 기준 뒤 Tunnel 장애 알림을 전송하고 사용자 서비스를 강제 재기동함. 한 번의 실패 뒤 정상 전환은 Telegram으로 알리지 않음. 복구 시도 횟수는 상태 파일로 보존함. 상태를 저장하지 못하면 중복·무한 복구를 방지하기 위해 이후 복구를 중단함. Supervisor 초기 host metrics 기록 실패는 감시 기동을 막지 않음. Portal PVC·Secret·운영 데이터·Caddy 설정·Tunnel ingress는 자동복구 대상이 아님. Tunnel 장애 알림을 1회 전송하는 데 성공한 경우에만 자동복구 작업은 이후 정상 전환에서 복구 알림을 1회 보냄. 장애 알림 전송이 실패하면 다음 임계치 충족 점검에서 장애 알림을 재시도함. 자격증명은 `window` 계정의 Windows Credential Manager 일반 자격 증명에서만 읽으며 값은 로그·상태 파일·문서에 기록하지 않음. GitHub Actions 외부 health 점검은 독립 보완 경로이므로 같은 장애에서 메시지가 중복될 수 있음.

## 긴급 재부팅 발동·취소 기준

`PersonalServer-EmergencyReboot`는 트리거 없이 등록되며, 자동복구가 WSL 유지 작업 또는 K3s 중 하나의 대상 복구를 3회 시도한 뒤에도 해당 항목이 비정상일 때만 시작함. Windows 부팅 시점부터 20분 유예가 있으며, 발동 후 6시간 cooldown 동안 재실행하지 않음. Tunnel·Portal·NodePort 단독 장애에는 사용하지 않음.

유예 중 취소가 필요하면 Windows PowerShell에서 60초 안에 다음 명령을 실행함.

```powershell
shutdown /a
```

재부팅 검증은 Windows 로그인 없이 수행함. 부팅 후 `PersonalServer-WSL-KeepAlive`가 `Running`인지, `post_boot_check` 결과가 `passed`인지, 외부 health를 10초 간격으로 3회 호출해 모두 HTTP 200인지 순서대로 확인함. 재부팅으로 상태를 복구하지 못하면 추가 재부팅을 반복하지 않고 기존 외부 상태 알림 및 운영자 수동 대응으로 이관함.

Windows PowerShell에서 자동복구 작업 상태를 확인함.

```powershell
$taskNames = 'PersonalServer-WSL-KeepAlive', 'personal-server-autostart'
$actionContracts = @{
  'PersonalServer-WSL-KeepAlive' = 'wsl.exe|Ubuntu-24.04|while true'
  'personal-server-autostart' = 'windows-bootstrap.ps1|-Supervisor'
}
foreach ($taskName in $taskNames) {
  Get-ScheduledTask -TaskName $taskName
  $taskXml = Export-ScheduledTask -TaskName $taskName
  $taskXml | Select-String 'BootTrigger|LogonType|Password|ExecutionTimeLimit|RestartOnFailure'
  $taskXml | Select-String $actionContracts[$taskName]
}
```

정상 기준은 `PersonalServer-WSL-KeepAlive`와 `personal-server-autostart`가 모두 `Running`이고, 두 작업의 내보낸 XML에 `BootTrigger`, `LogonType`의 `Password`, `ExecutionTimeLimit` `PT0S`, `RestartOnFailure`가 모두 포함되는 것임. KeepAlive XML은 `wsl.exe`, `Ubuntu-24.04`, `while true` action 계약을, Supervisor XML은 `windows-bootstrap.ps1`과 `-Supervisor` action 계약을 각각 포함해야 함. Daemon 재기동 검증은 Supervisor를 중지하지 않고 Daemon 자식만 종료한 뒤 Supervisor PID가 유지되고 새 Daemon PID가 생성되는지 확인함. 이 검증은 사용자 승인된 점검 창에서만 수행함.

## 재부팅 뒤 상태 확인

WSL에서 실행함.

```bash
cd /mnt/c/personal-server
systemctl --user is-active cloudflared-personal-server.service
sudo k3s kubectl -n personal-server get deploy,pod,pvc
failed=0
for attempt in 1 2 3; do
  http_code="$(curl --output /dev/null --silent --show-error --write-out '%{http_code}' https://len.pe.kr/health)"
  curl_exit=$?
  printf '외부 health %s회차: HTTP %s\n' "$attempt" "$http_code"
  if [ "$curl_exit" -ne 0 ] || [ "$http_code" != "200" ]; then
    failed=1
  fi
  if [ "$attempt" -lt 3 ]; then sleep 10; fi
done
exit "$failed"
```

`recovery-events.jsonl`에서 `post_boot_check`의 `passed` 결과를 확인한 뒤 위 외부 health 3회가 모두 HTTP 200이어야 함. 각 probe는 HTTP 상태 코드를 출력하며, 전송 실패 또는 HTTP 200 이외의 코드가 하나라도 있으면 세 번째 probe까지 완료한 뒤 nonzero로 종료함. 공개 주소가 실패하면 [Cloudflare Tunnel 운영 가이드](cloudflare-tunnel.md)의 1033·502 대응 절차를 따름.

## 자원 확인

```bash
cd /mnt/c/personal-server
docker stats
free -h
```

Windows 전체 메모리 사용량과 Docker·WSL 사용량은 다를 수 있음. 자원 압박이 지속될 때만 서비스별 사용량과 host metrics를 함께 확인함.

## 운영 경계

- `.env`, `data/`, Kubernetes Secret, PVC는 N100에만 보관하며 Git에 올리지 않음.
- Portal, K3s, Caddy, Cloudflare Tunnel은 안전 자동 배포 대상이 아님.
- N100 전원·네트워크·WSL 자체가 불가한 물리·호스트 장애는 자동복구·직접 Telegram 알림 범위 밖임.
- 자동 배포 범위와 수동 복구는 [N100 안전 자동 배포](n100-github-auto-deploy.md)를 따름.
- Mac에서 작업을 이어갈 때는 [N100 원격 개발](n100-remote-development.md)을 사용함.
