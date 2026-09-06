# Personal Server

Windows N100과 Ubuntu WSL2에서 운영하는 개인 서버임. 단순 서비스 배포를 넘어, 모니터링·장애 감지·자동복구·암호화 백업·복원 검증·Telegram 알림을 연결한 **개인 서버 SRE 운영 체계**를 구축함.

## 현재 구조

```text
Internet
  └─ Cloudflare Tunnel
       └─ Caddy (Docker)
            ├─ K3s NodePort → portal-web
            └─ Docker Compose → news · memo · books · system status

K3s monitoring → Prometheus · Grafana · Alertmanager · Telegram SRE relay
GitHub Actions → 5분 외부 health 점검 → Telegram 장애·복구 알림
```

| 구분 | 현재 운영 방식 |
|---|---|
| Portal·파일함·관리자·포트폴리오 | K3s `portal-web` + PVC 단일 writer |
| 뉴스·YouTube 메모·책 메모·차량관리 | Docker Compose |
| 공개 경로 | Cloudflare Tunnel → Caddy → 서비스 |
| 모니터링 | K3s Prometheus·Grafana, Telegram SRE 알림 |
| 백업 | Portal PVC 암호화 백업 및 복원 검증 |
| 외부 장애 감지 | GitHub Actions가 약 5분 간격으로 `https://len.pe.kr/health` 확인 |

## SRE 운영 체계

```text
서비스 실행
  ↓
Prometheus·Grafana로 K3s 상태 관측
  ↓
Alertmanager·SRE relay로 내부 이상 Telegram 알림

GitHub Actions가 외부 주소를 약 5분마다 별도 점검
  ↓
장애·복구 전환 시 Telegram 알림

Portal PVC 암호화 백업 → 원격 보관 → 복원 검증
```

| SRE 영역 | 적용 내용 |
|---|---|
| 관측성 | Prometheus·Grafana로 K3s 노드·Pod·PVC·서비스 상태 확인 |
| 장애 감지 | 내부 Prometheus 경고와 외부 GitHub Actions health 점검을 분리 |
| 알림 | Telegram으로 장애·복구·백업 결과를 한국어로 전달 |
| 복구 | K3s Pod 자동복구, HomeOps 제한형 컨테이너 복구, 재부팅 뒤 WSL 유지 |
| 데이터 보호 | Portal PVC 암호화 백업과 실제 복원 검증 |
| 안전 배포 | 허용된 Compose 서비스만 CI 성공 뒤 revision 고정 배포·health 검증·1회 rollback |

## 주요 기능

- 개인 포털, 파일 관리, 관리자 상태, 공개 포트폴리오
- Investing.com·Google News 수집 및 Telegram 뉴스 알림
- YouTube·독서 메모 관리
- Hyundai 연동 차량 상태·정비 Telegram 알림
- HomeOps 제한형 진단·복구, Grafana 상태 확인, Telegram SRE 알림

## 빠른 상태 확인

N100 WSL에서 실행함.

```bash
cd /mnt/c/personal-server
curl --fail --silent --show-error https://len.pe.kr/health
sudo k3s kubectl -n personal-server get deploy,pod,pvc
bash infra/k8s/tools/sre-health-audit.sh
```

각 백업·모니터링·Telegram relay의 상세 점검은 [운영 문서 색인](docs/README.md)을 사용함. 비밀번호·토큰·Secret 값은 문서나 명령 출력에 기록하지 않음.

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
| [N100 운영 환경](docs/n100-mt4-setup.md) | Windows·WSL2 자동 시작과 장애 확인 |
| [N100 안전 자동 배포](docs/n100-github-auto-deploy.md) | 허용 Compose 서비스의 CI 기반 배포 |
| [K3s 운영](infra/k8s/README.md) | Portal·Grafana·Telegram SRE·PVC 백업 |
| [공개 상태 Telegram 알림](docs/public-uptime-monitor.md) | 외부 장애·복구 알림 기준 |
