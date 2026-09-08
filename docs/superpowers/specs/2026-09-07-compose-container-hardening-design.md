# Compose 컨테이너 최소 권한 강화 설계서

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | Compose 컨테이너 최소 권한 강화 설계서 |
| 작성일 | 2026-09-07 |
| 목적 | N100 자동 배포 허용 서비스의 컨테이너 기본 실행 사용자를 root에서 전용 비관리 사용자로 전환함 |
| 적용 범위 | `crawler-worker`, `youtube-memo`, `book-memo` 이미지 |
| 제외 범위 | Portal, K3s, Caddy, 서버 기동·스케줄러, Secret·PVC·운영 데이터, `homeops-executor` |

## 핵심 요약

N100 자동 배포가 허용하는 일반 서비스 세 이미지에만 전용 UID/GID `10001`를 적용함. 이미지 빌드 단계의 패키지 설치는 root로 유지하고, 애플리케이션 소스는 해당 사용자 소유로 복사한 뒤 `USER 10001:10001`을 최종 이미지에 적용함.

N100 Compose override에 이미 적용된 읽기 전용 root filesystem, capability 제거, `no-new-privileges`는 유지함. 호스트 또는 volume의 소유권을 자동 변경하지 않으며, 권한 오류가 발생하면 해당 서비스 이미지를 이전 revision으로 되돌리는 것을 롤백 기준으로 함.

## 설계 결정

| 구분 | 결정 | 이유 |
|---|---|---|
| 대상 | 일반 서비스 세 개만 우선 적용 | Portal·K3s·Caddy와 Docker 소켓 보유 executor를 분리하여 운영 위험을 제한함 |
| 계정 | 이미지별 전용 system group/user, UID/GID `10001` 사용 | root 권한을 제거하면서 이미지 간 일관된 검사 기준을 유지함 |
| 파일 소유권 | 애플리케이션 복사는 `COPY --chown=10001:10001` 사용 | 읽기 전용 root filesystem에서도 애플리케이션 import 권한을 보장함 |
| 데이터 경로 | 기존 `/data/*` volume mount와 환경 변수 유지 | 데이터 이동·Secret 재시딩·PVC 변경을 발생시키지 않음 |
| 예외 | `homeops-executor`는 변경하지 않음 | Docker socket 접근은 사용자 전환만으로 권한을 축소하지 못하므로 별도 설계가 필요함 |
| 배포 | PR CI와 이미지 build 검증 후 N100 안전 배포 경로만 사용 | 허용 서비스 단위 health 확인과 rollback 계약을 재사용함 |

## 서비스별 경계

| 서비스 | 이미지 내 코드 경로 | 기존 쓰기 경로 | 이번 변경 | 배포 후 확인 |
|---|---|---|---|---|
| crawler-worker | `/app` | `/data/crawler-worker`, `/app/data/logs` | 전용 사용자·소유권 적용 | `/health`, 뉴스 DB·로그 쓰기 |
| youtube-memo | `/app` | `/data/youtube-memo`, `/app/data/logs` | 전용 사용자·소유권 적용 | `/health`, 메모 DB·로그 쓰기 |
| book-memo | `/app` | `/data/book-memo`, `/app/data/logs` | 전용 사용자·소유권 적용 | `/health`, 메모 DB·로그 쓰기 |
| car-care-worker | `/app/app` | `/data/car-care`, `/data/oauth` | 이번 전환에서 제외 | 기존 root 생성 OAuth token의 소유권·기능 상태 별도 설계 필요 |

## 오류 처리와 롤백

1. 이미지 build 또는 Compose healthcheck가 실패하면 배포를 중단함.
2. N100 안전 배포가 health 실패를 감지하면 기존 `deploy-n100-safe.sh`의 단일 rollback 계약을 사용함.
3. 데이터 volume의 `chown`, Secret 재생성, PVC 변경, 수동 데이터 복구는 이 작업의 자동 처리 대상이 아님.
4. car-care OAuth named volume은 기존 root 생성 token을 사용하는 상태이므로, 전용 사용자 전환과 health 의미를 별도 설계하기 전까지 변경하지 않음.

## 검증 기준

1. 대상 세 Dockerfile 모두 root가 아닌 전용 `USER 10001:10001`으로 종료함.
2. 소스 복사 단계가 전용 사용자 소유권을 명시함.
3. `homeops-executor` Dockerfile과 Docker socket 설정은 변경하지 않음.
4. 관련 정적 계약 테스트와 이미지 build가 통과함.
5. PR CI가 통과하고, N100 배포 직전에 변경 범위·예상 재시작·rollback 경로를 보고함.

## 확인 필요 사항

- 실제 N100 volume의 파일 소유권은 배포 직전 read-only 점검으로 확인 필요함.
- car-care OAuth named volume의 기존 token 소유권과 차량 연동 상태 검증은 다음 별도 보안 작업에서 확인 필요함.
