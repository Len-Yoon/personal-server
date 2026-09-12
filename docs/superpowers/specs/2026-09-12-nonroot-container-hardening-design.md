# 컨테이너 non-root 전환 설계

## 목적

Trivy DS-0002가 지적한 root 실행 Dockerfile 5개를 전용 UID/GID `10001:10001`로 전환함. 대상은 Caddy, car-care-worker, homeops-executor, portal-web, system-agent임.

## 범위

- 각 대상 Dockerfile은 전용 계정 생성, 애플리케이션 파일 소유권 설정, 명시적 `USER 10001:10001`을 적용함.
- Caddy는 Caddyfile과 host 포트 mapping을 유지하고, Compose에서 `NET_BIND_SERVICE`만 부여하여 container 내부 80/443 binding을 유지함.
- car-care-worker는 `/data/car-care`, `/data/oauth`의 초기 소유권을 맞추고 N100 배포 스크립트가 host bind mount의 `data/car-care`를 `10001:10001`로 정렬하도록 함.
- Portal은 K3s Deployment·copy/restore Pod에 `runAsNonRoot`, `runAsUser`, `runAsGroup`을 적용하고 PVC Pod의 `fsGroup`은 적용하지 않음. 기존 PVC 데이터의 소유권·mode는 자동 변경하지 않으며, non-root write probe와 rollback 경로로 호환성을 검증함. `fsGroup`은 PVC를 연결하지 않는 shadow smoke의 emptyDir에만 적용함.
- homeops-executor는 Docker TCP proxy 경계를 유지하고 socket/group 권한을 추가하지 않음. system-agent는 `/data` read-only mount를 유지함.

## 제외 범위

- docker-socket-proxy의 root 예외 및 Docker API allowlist 구조 변경.
- Caddyfile, Tunnel ingress, CronJob schedule/RBAC/Secret 값 변경.
- Portal PVC 및 운영 데이터의 일괄 chown/chmod. 권한 미충족 시 적용을 중단하고 별도 운영 판단으로 이관함.

## 안전 조건

- Caddy named volume은 운영 적용 전 read-only 권한 확인 후, 필요한 경우에만 사용자 승인된 1회 초기 소유권 정렬과 rollback 검증을 수행함.
- N100 적용 전 system-agent의 `/data` read/traverse와 Portal PVC non-root write probe를 비파괴로 실행함.
- 각 서비스는 이미지 build, Compose/K3s 계약 테스트, Trivy config scan, 실제 N100 health 검증을 통과해야 함.
- K3s·Compose 적용 후 외부 health는 10초 간격 3회 모두 HTTP 200이어야 함.

## 성공 기준

1. 대상 5개 Dockerfile 모두 DS-0002를 통과함.
2. 대상 프로세스가 runtime UID/GID 10001로 동작함.
3. Caddy TLS 저장소, Portal 파일·SQLite·보안 상태, car-care SQLite·OAuth, system-agent read-only 수집이 회귀하지 않음.
4. Docker socket proxy는 root 예외로 남되 기존 internal network·read-only socket mount·제한 endpoint 계약을 유지함.
