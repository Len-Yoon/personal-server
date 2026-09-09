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

`personal-server-autostart` 작업은 초기 시작 이후 3분 간격으로 WSL 유지, K3s, Portal, NodePort, Cloudflare Tunnel을 점검함. 같은 구성요소가 2회 연속 비정상일 때만 승인된 복구를 시도하며, 구성요소별 시도 횟수는 최대 3회임.

작업은 무기한 실행(`ExecutionTimeLimit PT0S`)되며, 비정상 종료 시 1분 간격으로 최대 3회 재시작함. 이 보강은 기존 BootTrigger, 실행 계정, 실행 명령을 유지한 상태에서 재시작 정책만 추가한 것임.

| 대상 | 자동복구 범위 |
|---|---|
| WSL 유지 작업 | 중지된 `PersonalServer-WSL-KeepAlive` 작업 시작 |
| K3s | 실행 중이 아닐 때만 시작. 실행 중인 K3s 재시작 금지 |
| Portal | K3s가 정상이고 Portal 가용 상태가 아닌 경우에만 rollout 재시작 |
| NodePort | 상태 확인만 수행. Portal 재시작으로 우회하지 않음 |
| Cloudflare Tunnel | 실행 중이 아닐 때만 Tunnel 실행 시도 |

복구 시도 횟수는 상태 파일로 보존함. 상태를 저장하지 못하면 중복·무한 복구를 방지하기 위해 이후 복구를 중단함. Portal PVC·Secret·운영 데이터·Caddy 설정·Tunnel ingress는 자동복구 대상이 아님. 자동복구 작업은 Telegram을 직접 발송하지 않으며, 외부 장애·복구 알림은 GitHub Actions 상태 점검이 담당함.

Windows PowerShell에서 자동복구 작업 상태를 확인함.

```powershell
Get-ScheduledTask -TaskName personal-server-autostart
Get-ScheduledTask -TaskName PersonalServer-WSL-KeepAlive
```

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
- 자동 배포 범위와 수동 복구는 [N100 안전 자동 배포](n100-github-auto-deploy.md)를 따름.
- Mac에서 작업을 이어갈 때는 [N100 원격 개발](n100-remote-development.md)을 사용함.
