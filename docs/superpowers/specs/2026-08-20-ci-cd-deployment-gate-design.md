# CI 성공 기반 N100 배포 게이트 설계

## 목적

`main` 코드가 CI를 통과한 경우에만 N100 자동 배포를 시작하도록 변경함. 배포 이후에는 Compose 상태와 서비스 health endpoint를 확인함.

## 현재 문제

CI와 `Deploy N100` workflow가 모두 `main` push를 트리거로 사용함. 따라서 CI 결과가 나오기 전에 N100 배포가 시작될 수 있음. CI에 HomeOps 실행기, HomeOps 알림, 뉴스 화면 라우트, 배포 스크립트 테스트가 빠져 있음.

## 변경 설계

1. CI workflow는 PR·`main` push 검증을 유지하고 누락된 HomeOps·뉴스 화면·배포 스크립트 테스트를 추가함.
2. N100 배포 workflow 트리거를 `CI` workflow 완료 이벤트로 변경함.
3. 배포는 `main`에서 발생한 CI가 `success`일 때만 실행함.
4. 기존 N100 runner·`scripts/deploy-n100.sh`·Compose 기동 목록은 변경하지 않음.
5. 배포 script 실행 뒤 workflow에서 Compose 상태와 공개 서비스의 `/health`, 컨테이너 내부 homeops-executor `/health`를 확인함.

## 제외 범위

- 자동 롤백, Windows·WSL·Docker 엔진 재시작을 추가하지 않음.
- N100 서버 기동 스크립트와 스케줄러를 수정하지 않음.
- Telegram 배포 알림은 이번 변경에 포함하지 않음.

## 검증 기준

- workflow 구조 테스트가 CI 성공 조건과 health check 단계를 확인함.
- CI matrix가 HomeOps·뉴스 화면·배포 스크립트 테스트를 포함함.
