# Repository Guidance

## 사용자 대화 언어

- 사용자에게 보내는 기본 안내, 진행 보고, 질문, 선택지는 한국어로 작성함.
- 사용자가 다른 언어를 명시적으로 요청한 경우에만 해당 언어를 사용함.
- 도구·프레임워크의 영문 고정 문구를 그대로 인용해야 할 때도, 먼저 한국어 설명을 제공함.

## 작업 범위 제한

- 서버 기동·스케줄러 코드는 기본적으로 수정하지 않음.
- 단, N100 자동복구 감시·복구 작업은 작업 시작 전 사용자 명시 승인을 받은 경우에만 아래 파일을 최소 범위로 수정할 수 있음.
  - `scripts/windows-bootstrap.ps1`
  - `scripts/verify_change_scope.py`
  - 자동복구 변경을 검증하는 관련 테스트 파일
  - `docs/codex-work-loop.md`
  - `docs/public-uptime-monitor.md`
- 자동복구 변경은 WSL, K3s, Portal 상태, Cloudflare Tunnel 감시·복구, 감시 프로세스 단일 실행·비정상 종료 재기동, 실패 횟수 제한, 중복 실행 방지, Cloudflare Tunnel 장애·복구 전환 Telegram 알림에 한정함.
- 허용 복구 동작은 기존 KeepAlive 작업 시작, inactive 상태 K3s 시작, 제한형 `portal-web` rollout restart, Cloudflare Tunnel 재기동으로 한정함.
- 허용 알림 동작은 Cloudflare Tunnel 장애·복구 상태 전환을 Telegram으로 1회씩 전달하는 것으로 한정함.
- K3s가 active인 상태에서 K3s 전체 restart를 수행하지 않음.
- Portal PVC·Secret·운영 데이터·Caddyfile·Tunnel ingress·Compose Portal writer는 계속 수정·삭제·재생성 금지함.
- 새 sudo 권한, 비밀번호, Telegram 토큰, Secret, 외부 자격 증명을 생성·저장·출력하지 않음.
- 단, N100 Cloudflare Tunnel 장애·복구 Telegram 알림에 한해 운영자가 Windows Credential Manager에 수동으로 사전 시딩한 Bot token과 Chat ID를 읽기 전용으로 조회할 수 있음.
- 저장소 코드·Git·문서·로그·상태 파일·환경 변수에는 Telegram Bot token·Chat ID를 생성, 출력, 복제하거나 평문으로 저장하지 않음. 전송 과정의 메모리에서만 사용하며, 알림 전송 실패 메시지에도 비밀값을 포함하지 않음.
- 자동복구 상태와 로그에는 비밀값을 기록하지 않음.
- 같은 구성요소의 자동복구가 3회 연속 실패하면 추가 재시작을 중단하고 기존 외부 상태 알림 경로로 이관함.
- 정상 상태에서는 Compose 서비스 또는 Portal을 주기적으로 재생성하지 않음.
- 적용 전후 독립 운영 검토와 실제 외부 health 검증을 필수로 수행함.
- 외부 health 검증은 `https://len.pe.kr/health`를 10초 간격으로 3회 호출하여 모두 HTTP 200인지 확인함.
- 자동복구 예외는 N100 안전 자동배포 대상이 아니며, 사용자 승인 없이 배포·push·병합하지 않음.

## 백업 자동화 예외

- Portal K3s PVC 백업에 한해 `systemd --user` timer/service 또는 K3s CronJob을 추가·수정할 수 있음. CronJob은 매일 백업·복원 검증을 실행하고, 성공·변경 없음·실패·미복구 결과를 Telegram SRE relay로 보고하는 범위에서만 허용함.
- 자동 실행은 하나의 방식만 선택해 중복 실행을 방지하며, `infra/k8s/tools/portal-pvc-backup-verify.sh --go`만 실행할 수 있음.
- rclone 설정 암호는 Git·평문 파일·환경 변수에 저장하지 않음. `systemd --user` 방식은 N100 사용자 전용 `systemd-creds` 암호화 자격 증명으로만 보관하고 실행 시 rclone 프로세스에만 전달함. CronJob 방식은 별도 승인된 Secret Manager 또는 SOPS/age 절차로 사전 시딩된 Kubernetes Secret만 참조할 수 있으며, 이 저장소의 도구가 비밀값을 생성·출력·복제하지 않음.
- sudo 비밀번호는 저장하지 않음. 기존의 제한된 `sudo -n k3s` 권한만 사용함.
- 백업 결과와 복구 실패는 Telegram SRE relay로 알림.

## 시간 처리 기준

- 시간 관련 신규 기능은 내부 저장·비교용 시각을 UTC의 시간대 인식 ISO 8601 값으로 유지함.
- 사용자 화면, 알림, 날짜 기반 판단은 KST(`Asia/Seoul`)를 기본 기준으로 사용함.
- 사용자에게 노출하는 시각은 `Asia/Seoul`로 변환한 `YYYY-MM-DD HH:MM`만 표시하며, `KST`, 요일, 초, UTC 원문을 노출하지 않음.

## Codex 작업 완료 루프

- 코드·설정 변경 전 변경 범위, 제외 범위, 성공 기준을 확인함.
- 기본 모델은 Sol을 사용하지 않으며, 조사·단순 구현·테스트는 Luna, 설계 및 운영·보안·rollback 검토는 Terra를 사용함.
- 검증은 위험 기반으로 변경 핵심 회귀 테스트, 관련 테스트 묶음, 정적 검사, 독립 검토를 적용하며, 모든 단계에서 전체 테스트를 반복하지 않음.
- 코드·설정 변경 전 변경 경로 파일을 준비하여 `python3 scripts/run_change_harness.py --input <변경경로파일> --agent-context`를 실행하고, 출력된 작업 상태와 다음 조치를 작업 컨텍스트에 반영함. Codex가 이 출력을 자동으로 작업 컨텍스트에 주입한다고 가정하지 않음.
- N100 자동복구 승인 작업은 `scripts/verify_change_scope.py`가 `scripts/windows-bootstrap.ps1`을 maintenance 검증 대상 자동화 파일로 분류하도록 관련 정책·테스트를 함께 갱신함.
- Git 변경 목록은 `git diff --name-status -z --find-renames <기준커밋> HEAD > <변경경로파일>`로 만들고, 하네스에는 `--input-format git-name-status-z`를 함께 전달함. 경로에 공백·한글·rename이 있어도 줄 단위 형식으로 변환하지 않음.
- 변경 범위와 직접 관련된 테스트 및 적용 가능한 정적 검사는 반드시 실행하고 통과해야 완료로 보고함.
- 전체 테스트는 변경이 공용 런타임·공용 라이브러리·CI 계약·배포 경로에 영향을 주거나, 관련 테스트만으로 회귀 위험을 판단할 수 없을 때 실행함.
- 전체 테스트에서 변경 범위와 무관한 기존 실패가 발생한 경우, 실패한 테스트·원인·변경 파일과의 비연관 근거를 완료 보고에 기록함. 관련 테스트와 정적 검사가 통과했고 독립 검토에서 비연관성이 확인되면, 사용자 승인 후 커밋·PR·병합을 진행할 수 있음.
- 변경 파일, 변경 기능, 공용 계약과 직접 관련된 테스트 실패 또는 원인 분리가 불가능한 실패는 반드시 차단으로 처리하며 커밋·PR·병합하지 않음.
- 테스트·정적 검사 후 실제 실행 결과를 `--check-result <검사명>=success` 또는 `--check-result <검사명>=failure`로 전달하여 `--agent-context`를 다시 실행함. 필요한 검증 결과가 누락되었거나 실패·차단 상태이면 완료로 보고하지 않음.
- 실제 모델 토큰 절감률은 작업별 JSONL 기록을 `python3 scripts/summarize_token_measurements.py --input <측정기록.jsonl>`로 집계함. 바이트 크기 절감률을 모델 토큰 절감률로 표현하지 않음.
- 실패 원인을 확인한 최소 수정만 수행하고, 동일 작업의 재검증은 최대 3회로 제한함.
- 범위 불명확, 금지 영역 접근, 보안·운영·배포 영향 판단 필요 시 작업을 중단하고 사용자 확인을 요청함. 단, 위 N100 자동복구 예외의 승인 범위와 안전 경계를 모두 충족하면 진행 가능함.
- 완료 보고에는 변경 내용, 변경 파일, 관련 테스트·정적 검사 결과, 전체 테스트 실행 여부와 결과, 미검증 항목, 관련 없는 기존 실패의 근거, 확인 필요 사항을 포함함.
- 기능 영향 없는 문서화·문구·서식 변경, 읽기 전용 조사, 단순 질문은 주 에이전트 1명이 처리할 수 있음. 완료 보고에 하위 에이전트 미사용 사유를 기록함.
- 단일 서비스의 기능 변경·버그 수정은 주 에이전트와 독립 검토 에이전트로 역할을 분리함.
- 리팩터링, 2개 이상 서비스·모듈 변경, 3개 초과 기능 구현·테스트·설정 파일을 수정하는 변경은 주 에이전트·구현 에이전트·검토 에이전트 3명 분업을 필수로 적용함.
- CI/CD·보안·DB 마이그레이션·배포 영향 작업은 전문 검토 에이전트를 필수로 포함해 최대 4명으로 수행함.
- 같은 파일을 수정하는 구현 에이전트는 병렬로 실행하지 않으며, 작업 시작 전 역할·범위·성공 기준을 선언하고 완료 보고에 역할별 결과를 기록함.
- 병합된 PR은 CI·배포 성공 및 작업공간 무변경을 확인한 뒤 원격·로컬 작업 브랜치와 분리 작업공간을 자동 정리함. 미병합 또는 재사용 예정 브랜치는 삭제하지 않음.
- 상세 절차는 `docs/codex-work-loop.md`를 따름.

## Commit Messages

- 커밋 메시지는 기본적으로 한글로 작성함.
- 형식은 가능하면 `유형: 설명` 형태를 따름.
- 예시: `test: 서비스별 테스트 import 충돌 해결`, `chore: 홈 링크 정리`

## N100 안전 자동 배포 정책 예외

N100 안전 자동 배포 작업에 한해 변경 분류 계약(`scripts/classify-n100-safe-deployment.py`), 안전 배포·health 검증 스크립트 및 `.github/workflows/deploy-n100.yml`만 추가·수정할 수 있음.
자동 대상은 `crawler-worker`, `youtube-memo`, `book-memo`, `car-care-worker`의 허용된 Compose 변경으로 제한함. Portal, K3s, Kubernetes Secret·PVC·운영 데이터, Caddy, 서버 bootstrap 및 scheduler는 계속 제외하며, 해당 경로가 섞인 변경은 배포하지 않고 차단함.

N100 자동복구 예외는 위 안전 자동배포 정책의 대상이 아니며, 자동복구 변경이 포함된 브랜치는 별도 운영 검토와 사용자 승인 없이는 배포하지 않음.
