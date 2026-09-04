# N100 원격 개발 환경 운영 절차

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | N100 원격 개발 환경 운영 절차 |
| 작성일 | 2026-09-04 |
| 기준 자료 | `scripts/n100-remote-dev.sh`, `scripts/n100-remote-dev-remote.sh` |
| 목적 | Mac에서 N100 WSL의 지속형 Codex 작업을 안전하게 시작·확인함 |
| 비고 | 서버 기동·스케줄러·K3s·Compose·Caddy·터널 변경은 범위에서 제외함 |

## 핵심 요약

Mac 제어 명령은 SSH alias `n100-codex`를 사용함. 현재 Mac의 `~/.ssh/id_ed25519_n100` 키는 해당 alias로 이미 인증되는 상태이므로, 정상 연결 시 `keygen` 또는 Windows 키 재등록은 수행하지 않음. 직접 IP를 지정하면 SSH config의 alias 설정이 적용되지 않을 수 있으므로 기본 alias를 유지함.

원격 실행기는 WSL의 고정 tmux 세션 `personal-server-codex-dev`에서 동작함. 작업 시작 시 전용 WSL worktree를 생성·재사용하며, 원본 `/mnt/c/personal-server`의 dirty source tree는 수정하지 않음.

## 사전 조건 및 최초 등록

### 1. Mac SSH 상태 확인

```bash
test -f ~/.ssh/id_ed25519_n100
ssh -i ~/.ssh/id_ed25519_n100 -o BatchMode=yes -o PasswordAuthentication=no n100-codex true
```

위 연결이 성공하면 기존 인증을 사용함. 실패하거나 키가 없는 경우에만 다음 명령으로 공개키를 생성·표시함.

```bash
bash scripts/n100-remote-dev.sh keygen
```

Windows에서 최초 1회만 관리자 PowerShell로 공개키를 `%ProgramData%\ssh\administrators_authorized_keys`에 등록함. `keygen`이 출력한 공개키를 사용하며, 개인키·비밀번호·토큰을 파일이나 로그에 복사하지 않음. 이미 인증되는 현재 환경에서는 이 등록 절차를 반복하지 않음.

### 2. 연결 사전점검

저장소 루트에서 실행함.

```bash
bash scripts/n100-remote-dev.sh preflight
```

`n100_remote_dev=PASS`를 확인한 뒤 작업을 시작함. SSH, WSL, 저장소, Git, tmux, Codex 및 GitHub 인증 상태 중 하나라도 확인되지 않으면 작업을 시작하지 않음.

## 작업 실행 및 확인

작업 지시문은 Mac에 있는 일반 파일의 절대 경로로 전달함. 파일은 64 KiB 이하이어야 함.

```bash
bash scripts/n100-remote-dev.sh start --task-file /absolute/path/task.txt
bash scripts/n100-remote-dev.sh status
bash scripts/n100-remote-dev.sh logs
```

원격 상태·로그는 WSL의 `~/.local/state/personal-server/n100-dev`에 유지됨. 실행 작업은 `~/.local/share/personal-server/n100-dev-worktree`에서 수행되며, 전용 worktree가 없을 때 `main` 기준으로 생성됨. 기존 전용 worktree가 dirty하거나 다른 저장소에 연결되어 있으면 새 작업을 시작하지 않음.

작업이 정상 종료된 뒤에도 `status`와 `logs`로 종료 코드와 최근 로그를 확인할 수 있음. 작업을 중단해야 할 때만 고정 tmux 세션을 종료함.

```bash
bash scripts/n100-remote-dev.sh stop
```

`stop`은 `personal-server-codex-dev` tmux 세션만 종료하며 원격 파일, 컨테이너, Kubernetes 리소스, Windows 서비스에는 영향을 주지 않음.

## 안전 경계

- Codex 작업은 로컬 변경·테스트·커밋까지만 허용함. push, pull request 생성, merge, deploy는 수행하지 않으며 필요한 경우 사용자 승인 필요함.
- 서버 기동, scheduler, K3s, Compose, Caddy, tunnel 리소스는 변경하지 않음.
- 실행기는 sudo 비밀번호, rclone 비밀번호/설정, GitHub 비밀번호·토큰, Codex 비밀번호·토큰을 자동으로 저장하거나 대리 입력하지 않음. 해당 값은 Git, 상태 파일, 로그에 기록하지 않음.
- `status`는 상태 정보만 표시함. `logs`는 Codex 표준출력의 마지막 200줄을 그대로 표시하므로, 작업 지시문이나 실행 출력에 비밀값을 넣지 말고 로그를 외부에 붙여넣지 않음.

## Mac 절전 또는 SSH 끊김 후 복구

SSH 연결이 끊겨도 원격 tmux 작업은 계속 실행됨. Mac이 깨어난 뒤 저장소 루트에서 다음 순서로 재접속함.

```bash
bash scripts/n100-remote-dev.sh preflight
bash scripts/n100-remote-dev.sh status
bash scripts/n100-remote-dev.sh logs
```

`preflight`가 실패하면 네트워크·SSH alias·N100 전원 상태를 확인한 뒤 다시 실행함. `status`가 `session=active`이면 기존 작업을 중복 시작하지 않음. 세션이 inactive이면 상태 파일의 종료 코드와 로그 마지막 부분을 확인하고, 필요한 경우 새 절대 경로 작업 파일로 `start`를 다시 수행함.

## 확인 필요 사항

- Windows 관리자 키 등록이 필요한지 여부는 `n100-codex` alias의 BatchMode SSH 연결 결과로 확인함.
- N100이 절전·재부팅되어 WSL 자체가 종료된 경우에는 SSH 재연결만으로 작업이 복구되지 않을 수 있음. `preflight` 결과와 `status`를 기준으로 재실행 여부를 판단함.

## 후속 조치

작업 완료 후 `status`·`logs`로 결과를 확인하고, 변경사항의 push·PR·merge·deploy는 사용자 승인 후 별도 절차로 수행함.
