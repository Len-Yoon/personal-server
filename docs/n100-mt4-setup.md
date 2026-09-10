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

Windows 로그인 뒤 `PersonalServer-WSL-KeepAlive` 작업이 WSL을 유지함. WSL이 유지되면 사용자 `cloudflared-personal-server.service`, K3s, Docker Compose가 실행 상태를 유지함.

Windows PowerShell에서 확인함.

```powershell
Get-ScheduledTask -TaskName PersonalServer-WSL-KeepAlive
wsl -l -v
```

정상 기준은 작업 상태가 `Running`, `Ubuntu-24.04` 상태가 `Running`임. 로그인 전에는 사용자용 WSL 서비스와 Tunnel이 시작되지 않을 수 있음.

## 제한형 자동복구

`personal-server-autostart` 작업은 Windows 시작 시 `-Supervisor`를 실행함. Supervisor는 중복 실행을 잠금으로 막고 초기 120초 안정화 대기 후 `-Daemon` 자식을 하나만 시작함. Daemon은 3분 간격으로 상태를 점검함. Daemon이 비정상 종료되면 Supervisor가 15초 뒤 새 Daemon을 시작하며, 60초 안에 3회 연속 종료되면 60초 backoff 뒤 다시 시도함. Daemon도 별도 잠금으로 중복·레거시 직접 실행을 막음. 2회 연속 health 실패 시 구성요소별 승인된 복구를 시도하고, 핵심 복구가 3회 연속 실패한 경우에만 긴급 재부팅 승격을 검토함. 긴급 재부팅은 Tunnel·Portal·NodePort 단독 장애에 사용하지 않음.

예약 작업은 무기한 실행(`ExecutionTimeLimit PT0S`)되며, Supervisor 자체가 비정상 종료하면 1분 간격으로 최대 3회 재시작함. 예약 작업의 실행 인자는 `windows-bootstrap.ps1 -Supervisor`여야 함. 이 보강은 기존 BootTrigger와 실행 계정을 유지하면서, Supervisor·Daemon 단일 실행 구조와 재시작 정책을 적용한 것임.

| 대상 | 자동복구 범위 |
|---|---|
| WSL 유지 작업 | 중지된 `PersonalServer-WSL-KeepAlive` 작업 시작 |
| K3s | 실행 중이 아닐 때만 시작. 실행 중인 K3s 재시작 금지 |
| Portal | K3s가 정상이고 Portal이 비가용일 때 제한형 rollout 재시작 |
| NodePort | 상태 확인만 수행. 자동 재시작 금지 |
| Cloudflare Tunnel | 사용자 서비스 시작·재시작 허용. 호스트 긴급 재부팅은 금지 |

Tunnel은 로컬 NodePort·`cloudflared` 서비스·Tunnel 프로세스·공개 `https://len.pe.kr/health`가 모두 정상일 때만 정상으로 판정함. NodePort가 비정상이면 Tunnel 상태는 보류하며 Tunnel Telegram 알림·재기동을 하지 않음. 반대로 로컬 상태는 정상인데 공개 health만 실패하면 기존 2회 연속 실패 기준 뒤 Tunnel 사용자 서비스를 강제 재기동함. 복구 시도 횟수는 상태 파일로 보존함. 상태를 저장하지 못하면 중복·무한 복구를 방지하기 위해 이후 복구를 중단함. Supervisor 초기 host metrics 기록 실패는 감시 기동을 막지 않음. Portal PVC·Secret·운영 데이터·Caddy 설정·Tunnel ingress는 자동복구 대상이 아님. Tunnel 장애 알림을 1회 전송하는 데 성공한 경우에만 자동복구 작업은 이후 정상 전환에서 복구 알림을 1회 보냄. 장애 알림 전송이 실패하면 다음 점검에서 장애 알림을 재시도함. 자격증명은 `window` 계정의 Windows Credential Manager 일반 자격 증명에서만 읽으며 값은 로그·상태 파일·문서에 기록하지 않음. GitHub Actions 외부 health 점검은 독립 보완 경로이므로 같은 장애에서 메시지가 중복될 수 있음.

## 긴급 재부팅 발동·취소 기준

`PersonalServer-EmergencyReboot`는 트리거 없이 등록되며, 자동복구가 WSL 유지 작업 또는 K3s 중 하나의 대상 복구를 3회 시도한 뒤에도 해당 항목이 비정상일 때만 시작함. Windows 부팅 시점부터 20분 유예가 있으며, 발동 후 6시간 cooldown 동안 재실행하지 않음. Tunnel·Portal·NodePort 단독 장애에는 사용하지 않음.

유예 중 취소가 필요하면 Windows PowerShell에서 60초 안에 다음 명령을 실행함.

```powershell
shutdown /a
```

재부팅 뒤 `PersonalServer-WSL-KeepAlive`와 K3s 상태, 사용자 Tunnel 서비스, 외부 health를 순서대로 확인함. 재부팅으로 상태를 복구하지 못하면 추가 재부팅을 반복하지 않고 기존 외부 상태 알림 및 운영자 수동 대응으로 이관함.

Windows PowerShell에서 자동복구 작업 상태를 확인함.

```powershell
Get-ScheduledTask -TaskName personal-server-autostart
Get-ScheduledTask -TaskName PersonalServer-WSL-KeepAlive
Export-ScheduledTask -TaskName personal-server-autostart | Select-String 'ExecutionTimeLimit|RestartOnFailure|-Supervisor'
```

정상 기준은 `personal-server-autostart`가 `Running`이고, 내보낸 작업 XML에 `ExecutionTimeLimit` `PT0S`, `RestartOnFailure`, `-Supervisor`가 모두 포함되는 것임. Daemon 재기동 검증은 Supervisor를 중지하지 않고 Daemon 자식만 종료한 뒤 Supervisor PID가 유지되고 새 Daemon PID가 생성되는지 확인함. 이 검증은 사용자 승인된 점검 창에서만 수행함.

## 재부팅 뒤 상태 확인

WSL에서 실행함.

```bash
cd /mnt/c/personal-server
systemctl --user is-active cloudflared-personal-server.service
sudo k3s kubectl -n personal-server get deploy,pod,pvc
curl --fail --silent --show-error https://len.pe.kr/health
```

공개 주소가 실패하면 [Cloudflare Tunnel 운영 가이드](cloudflare-tunnel.md)의 1033·502 대응 절차를 따름.

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
