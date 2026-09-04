# N100 원격 개발 환경 설계

## 목적

맥북의 개발자가 SSH를 통해 N100 Windows/WSL 작업 환경에서 Codex 작업을 시작하고, SSH 연결이 끊겨도 작업·로그·결과를 유지하며 다시 확인할 수 있게 함.

## 범위

| 구분 | 포함 | 제외 |
|---|---|---|
| 연결 | 전용 Ed25519 SSH 키 생성 및 Windows 등록 안내 | 비밀번호·개인키·토큰을 Git 또는 로그에 저장 |
| 실행 | WSL의 tmux 세션에서 Codex 비대화형 작업 실행 | system 서비스 등록, 부팅 시 자동 실행 |
| 관리 | start, status, logs, stop, preflight | 서버 기동, 스케줄러, K3s·Compose·Caddy·터널 변경 |
| Git | 로컬 변경·테스트·커밋까지 수행하도록 작업 지시 | push, PR 생성·병합, 배포 자동 실행 |

## 구조

`scripts/n100-remote-dev.sh`는 macOS에서 실행하는 제어 명령임. 고정된 `window@192.168.45.32` SSH 대상에 전용 키를 사용해 Windows의 `wsl.exe`를 실행하고, 저장소 내 `scripts/n100-remote-dev-remote.sh`를 호출함.

WSL 실행기는 사용자 홈의 `~/.local/state/personal-server/n100-dev`에 작업 지시문, 실행 로그, 종료 상태만 보관함. Codex는 이 상태 디렉터리를 사용해 tmux 세션 안에서 실행되며, 작업 지시문에 외부 변경 금지 규칙을 덧붙임. 비밀값, sudo 암호, rclone 암호, API 토큰은 읽거나 저장하지 않음.

## 명령 계약

| 명령 | 동작 | 성공 기준 |
|---|---|---|
| `keygen` | macOS 전용 SSH 키를 생성하고 Windows 등록용 공개키를 표시 | 개인키 0600, 공개키 생성 |
| `preflight` | SSH·WSL·저장소·git·tmux·codex·GitHub 인증을 읽기 전용 확인 | 필요한 도구와 인증 상태가 모두 확인됨 |
| `start --task-file FILE` | 작업 파일을 WSL 상태 디렉터리에 전송하고 tmux Codex 세션 시작 | 세션 이름과 로그 경로 반환 |
| `status` | 세션 상태, 마지막 종료 코드, 현재 브랜치를 표시 | 상태를 표시하고 비밀값을 출력하지 않음 |
| `logs` | 마지막 실행 로그의 제한된 뒷부분 표시 | 활성/종료 작업의 진단 가능 |
| `stop` | 실행 중인 tmux 세션만 종료 | Compose·K3s·Windows 서비스에 영향 없음 |

## 안전 경계

- 명령은 `window@192.168.45.32`와 `Ubuntu-24.04`를 기본값으로 하되 환경 변수로만 명시적으로 재정의 가능함.
- SSH 옵션은 전용 키·BatchMode·StrictHostKeyChecking을 사용하고, 암호 인증을 시도하지 않음.
- `start`는 절대 경로의 일반 파일만 작업 지시문으로 허용하며 64 KiB를 초과하면 거부함.
- tmux 세션명·상태 경로·저장소 경로는 고정함. 인자로 전달한 셸 코드는 실행하지 않음.
- Codex 프롬프트에는 로컬 개발·테스트·커밋만 허용하고 push/PR/병합/배포·운영 리소스 변경은 중단 후 보고하도록 고정함.
- stop은 tmux 대상 세션만 종료하며 원격 파일·컨테이너·Kubernetes 리소스를 삭제하지 않음.

## 오류 처리

- SSH 키가 없거나 권한이 안전하지 않으면 키 등록 절차를 표시하고 종료함.
- SSH·WSL·도구·GitHub 인증 preflight 실패는 어떤 tmux 작업도 시작하지 않음.
- 이미 실행 중인 세션이 있으면 새 작업을 시작하지 않음.
- Codex 종료 코드는 상태 파일에 기록하고 로그를 유지함.
- 네트워크 단절은 tmux 작업을 중단하지 않으며 다음 `status`/`logs`에서 확인 가능해야 함.

## 검증

- 호스트 스크립트의 인자·키 권한·작업 파일·원격 명령 생성·안전 프롬프트를 단위 테스트함.
- WSL 스크립트의 preflight·중복 세션 거부·상태 출력·stop 범위를 셸 단위 테스트함.
- 두 스크립트의 `bash -n` 검사와 변경 범위 하네스를 실행함.

## 확인 필요 사항

Windows의 기존 `authorized_keys`에는 현재 맥북이 보유하지 않은 키의 공개키가 들어 있어 SSH 인증이 실패함. 새 키 생성 뒤 Windows에 공개키를 한 번만 등록해야 실제 원격 설치·실행이 가능함.
