# Personal Server

Windows N100과 Ubuntu WSL2에서 운영하는 개인용 서비스 허브임. 파일·기록·뉴스·차량관리·서버 상태를 한 곳에서 사용하고, 이를 모니터링·자동복구·백업·Telegram 알림을 갖춘 **SRE 운영 체계**로 관리함.

## 개인 서버 기능

| 기능 | 제공 내용 | 실행 위치 |
|---|---|---|
| Portal | 자주 쓰는 서비스의 단일 진입점, 관리자 상태, 공개 포트폴리오 | K3s `portal-web` |
| File Manager | 파일 업로드·정리·검색·ZIP 다운로드 | K3s `portal-web` + PVC |
| News Hub | Investing.com·Google News 수집, 나스닥 관련성 분류, Telegram 중요 뉴스 알림 | `crawler-worker` |
| YouTube Memo | 영상 링크·타임스탬프·메모 기록 | `youtube-memo` |
| Book Memo | 책 검색·목차·독서 메모 관리 | `book-memo` |
| 차량관리 | Hyundai 연동 차량 상태·정비 주기·운행 종료 Telegram 알림 | `car-care-worker` |
| HomeOps | 제한된 컨테이너 진단과 승인된 복구 작업 | `system-agent` + `homeops-executor` |

## 주요 화면

| Investing.com 뉴스 수집 | 차량 관리 Telegram |
|---|---|
| RSS 수집 결과, Telegram 알림 분류, 원문 이동이 가능한 기사 목록을 확인함.<br><br><img src="docs/images/news-hub.png" alt="Investing.com 뉴스 수집 결과와 기사 목록" width="460"> | 운행 기록과 누적 주행거리, 주행 가능 거리, 소모품 정비 시점을 Telegram으로 확인함.<br><br><img src="docs/images/car-care-telegram-status.png" alt="Telegram 차량 관리 운행 및 정비 상태" width="460"> |

| YouTube Memo | Book Memo |
|---|---|
| 영상 링크 등록과 저장된 영상별 메모 수를 확인함.<br><br><img src="docs/images/youtube-memo.png" alt="YouTube Memo 영상 등록과 저장된 영상 목록" width="460"> | 책 검색, 내 책장, 목차 체크, 장별 코멘트 기록을 한 화면에서 확인함.<br><br><img src="docs/images/book-memo.png" alt="Book Memo 책장과 목차 체크 및 코멘트 기능" width="460"> |

| 파일함 | 관리자 상태 |
|---|---|
| 파일·폴더 생성, 업로드, 다운로드, 검색, 정렬과 선택 삭제를 지원함.<br><br><img src="docs/images/file-manager.png" alt="파일함의 파일 목록과 업로드 및 검색 기능" width="460"> | 서버 자원, 서비스 Health, 승인형 HomeOps 조치 이력을 확인함.<br><br><img src="docs/images/admin-status.png" alt="관리자 상태와 HomeOps 운영 보조" width="360"> |

## 현재 구조

![Personal Server 현재 운영 구조](docs/images/personal-server-architecture-v2.svg)

| 구분 | 현재 운영 방식 |
|---|---|
| Portal·파일함·관리자·포트폴리오 | K3s `portal-web` + PVC 단일 writer |
| 뉴스·YouTube 메모·책 메모·차량관리 | Docker Compose |
| 공개 경로 | Cloudflare Tunnel → Caddy → 서비스 |
| 모니터링 | K3s Prometheus·Grafana, Telegram SRE 알림 |
| 백업 | Portal PVC 암호화 백업 및 복원 검증 |
| 외부 장애 감지 | GitHub Actions가 약 5분 간격으로 `https://len.pe.kr/health` 확인 |

## 빠른 상태 확인

N100 WSL에서 실행함.

```bash
cd /mnt/c/personal-server
curl --fail --silent --show-error https://len.pe.kr/health
sudo k3s kubectl -n personal-server get deploy,pod,pvc
bash infra/k8s/tools/sre-health-audit.sh
```

상세 점검 기준과 장애 대응은 [운영 문서 색인](docs/README.md)을 사용함. 비밀번호·토큰·Secret 값은 문서나 명령 출력에 기록하지 않음.

## SRE 운영 체계

```text
서비스 실행
  ↓
내부 관측·외부 health 점검·제한형 자동복구·백업 복원 검증
  ↓
Telegram 알림과 운영 문서 기반 대응
```

| SRE 영역 | 적용 내용 |
|---|---|
| 관측성 | Prometheus·Grafana로 K3s 노드·Pod·PVC·서비스 상태 확인 |
| 장애 감지 | 내부 Prometheus 경고와 외부 GitHub Actions health 점검을 분리 |
| 알림 | N100 Tunnel 전환, 외부 health, 내부 경고·백업 결과를 Telegram으로 한국어 전달 |
| 복구 | K3s Pod 자동복구, HomeOps 제한형 컨테이너 복구, 재부팅 뒤 WSL 유지, N100 제한형 자동복구 |
| 데이터 보호 | Portal PVC 암호화 백업과 실제 복원 검증 |
| 안전 배포 | 허용된 Compose 서비스만 CI 성공 뒤 revision 고정 배포·health 검증·1회 rollback |
| 공급망 보안 | GitHub Actions 외부 action을 full SHA로 고정하고, Caddy를 제외한 관리 대상 Python Docker base image 8개를 digest로 고정함. Trivy filesystem/config scan은 HIGH·CRITICAL 결과를 차단하며 CI에서 검증함 |

개인 서버 기능과 함께 장애 감지·복구·데이터 보호를 운영하는 구성이 핵심임. 자동복구의 세부 조건·제한·예외는 [N100 운영 환경](docs/n100-mt4-setup.md)과 [운영 참조](docs/operations-reference.md)를 따름.

## 컨테이너 실행 보안

`caddy`, `car-care-worker`, `homeops-executor`, `portal-web`, `system-agent`는 전용 UID/GID `10001:10001`로 실행함. Caddy는 내부 80·443 포트 binding에 필요한 `NET_BIND_SERVICE` capability만 추가로 사용함.

Portal은 K3s 단일 writer와 PVC를 사용하므로 non-root 이미지 교체 전에 파일·상태 PVC의 UID/GID `10001:10001` 읽기·쓰기·디렉터리 접근 권한을 사전검증함. 권한이 충족되지 않으면 Deployment를 전환하지 않으며, PVC 권한 정렬은 자동 배포 대상이 아닌 별도 운영 작업으로 처리함.

## 모니터링 예시

Grafana에서 K3s 네임스페이스별 CPU·메모리 사용량, Pod 수, 요청량·제한값을 확인하는 예시 화면임.

![Grafana K3s 모니터링 화면](docs/images/grafana-k3s-overview.png)

## 개발과 배포

```text
기능 브랜치 → 테스트·독립 검토 → PR → 사용자 병합 승인 → main
```

`crawler-worker`, `youtube-memo`, `book-memo`, `car-care-worker`의 허용된 변경만 N100 안전 자동 배포 대상임. Portal, K3s, Caddy, Secret, PVC, 운영 데이터는 자동 배포 대상이 아니며 별도 운영 절차를 사용함.

병합된 변경은 CI·배포·health 검증과 작업공간 정리까지 확인함. CI artifact와 운영 증적은 90일 보관하며, 장기 보관이 필요한 자료는 별도 증적 저장소로 이전함.

## 검증

```bash
python3 tests/run_service_tests.py
python3 -m unittest tests.test_documentation_index -v
```

## 운영 문서

| 문서 | 내용 |
|---|---|
| [운영 문서 색인](docs/README.md) | 현재 운영 문서의 단일 진입점 |
| [운영 참조](docs/operations-reference.md) | 서비스 구조·공개 경로·일상 점검 |
| [N100 운영 환경](docs/n100-mt4-setup.md) | Windows·WSL2 자동 시작, 제한형 자동복구와 장애 확인 |
| [N100 안전 자동 배포](docs/n100-github-auto-deploy.md) | 허용 Compose 서비스의 CI 기반 배포 |
| [K3s 운영](infra/k8s/README.md) | Portal·Grafana·Telegram SRE·PVC 백업 |
| [공개 상태 Telegram 알림](docs/public-uptime-monitor.md) | 외부 장애·복구 알림 기준 |
