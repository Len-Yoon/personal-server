# 보안 P1·P2 하드닝 설계

## 1. 목적

운영 Compose 경로에서 개발용 쓰기 마운트와 reload를 제거하고, HomeOps의 raw Docker socket 접근을 제한형 socket proxy로 축소하며, 파일함 자원 고갈과 공급망 자동화의 잔여 위험을 줄임.

## 2. 범위

| 구분 | 변경 내용 |
|---|---|
| P1 | 개발 Compose와 N100 운영 Compose의 코드 마운트·실행 명령 분리 |
| P1 | HomeOps 실행기가 Docker socket proxy만 사용하도록 변경하고 proxy는 필요한 Docker API만 노출 |
| P2 | 다중 업로드와 ZIP 다운로드에 파일 수·누적 바이트 제한 및 통제된 오류 추가 |
| P2 | Caddy 이미지와 Cloudflare 모듈 버전 고정, Dependabot Python·Caddy 대상 추가, Trivy 고위험 결과 차단 |

## 3. 제외 범위

- N100 실제 배포, 외부 health 호출, Secret·PVC·운영 데이터 변경
- HomeOps 기능의 허용 서비스·재시작 정책 변경
- HSTS, 세션 저장 방식, Cloudflare 실제 프록시 CIDR 설정

## 4. 설계 결정

### 4.1 Compose 실행 경계

기본 `docker-compose.yml`은 개발 전용으로 유지하되, N100 override에서 코드 bind mount를 named volume으로 덮어쓰고 reload 명령을 제거함. 운영 컨테이너는 이미지에 포함된 코드만 실행하며, 상태·업로드 데이터 마운트만 쓰기 가능으로 유지함.

### 4.2 Docker socket 경계

새 socket proxy가 host Docker socket을 유일하게 마운트함. HomeOps 실행기는 proxy의 TCP endpoint만 사용하며, Docker SDK는 해당 endpoint를 통해 현재 허용된 컨테이너 조회·상태·로그·stats·restart에 필요한 API만 호출함. proxy는 privilege, volume, exec, image build, network, secret 관련 API를 노출하지 않음.

### 4.3 파일함 자원 제한

환경 변수로 단일 파일 제한과 별도로 다중 업로드 파일 수·누적 바이트, ZIP 대상 파일 수·원본 누적 바이트를 제한함. 제한 위반은 임시 파일을 정리하고 HTTP 400으로 반환함. 기본값은 기존 단일 파일 최대치와 N100 메모리·tmpfs 제약을 고려해 보수적으로 설정함.

### 4.4 공급망 자동화

Caddy builder/runtime image와 Cloudflare DNS module을 immutable digest 또는 commit으로 고정함. Dependabot은 모든 Python requirements 경로와 Caddy Dockerfile을 포함함. Trivy는 CRITICAL/HIGH 결과 시 실패하도록 하되 MEDIUM은 보고로 유지함.

## 5. 성공 기준

- N100 Compose에서 서비스 코드 경로는 쓰기 가능 bind mount가 아님.
- HomeOps 실행기는 `/var/run/docker.sock`을 직접 마운트하지 않음.
- 허용되지 않은 Docker API는 socket proxy에서 차단됨.
- 업로드·ZIP 총량 초과는 파일을 남기지 않고 통제된 오류가 됨.
- Caddy 및 Python 이미지·module 업데이트 대상이 CI와 Dependabot에 포함됨.
- 관련 단위·구성 계약 테스트가 통과함.
