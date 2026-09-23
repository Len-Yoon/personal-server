# Personal Server

파일, 읽은 책, 영상 메모, 시장 뉴스, 차량 정비 기록을 직접 관리하기 위해 만든 개인 서버입니다. N100 미니 PC 한 대에서 웹 서비스와 백그라운드 작업을 운영하고 있습니다.

처음에는 필요한 정보를 한곳에서 보고 저장하는 기능에 집중했습니다. 서비스가 늘어나면서 검색 대상의 응답 실패, 동시 저장, 알림 재처리, 컨테이너 교체 중 데이터 보존 같은 문제도 다루게 됐습니다. 이 저장소에서 애플리케이션 코드부터 배포 스크립트, 모니터링 설정, 백업과 복원 검증까지 함께 관리하고 있습니다.

## 무엇을 만들었는지

Portal을 시작점으로 뉴스와 메모를 찾아보고, 파일을 관리하며, 관리자 화면에서 서버 상태를 확인할 수 있습니다. 차량 상태와 정비 알림은 웹 화면 대신 평소 사용하는 Telegram으로 받습니다.

| 서비스 | 사용 흐름 | 구현 내용 |
|---|---|---|
| Portal | 한 화면에서 서비스에 접근하고 저장한 자료 검색 | 뉴스·영상·책 전체 검색, 관리자 상태, 공개 포트폴리오 |
| 파일함 | 파일을 올리고 폴더로 정리한 뒤 검색·다운로드 | 다중 업로드, 파일·폴더 관리, ZIP 다운로드, 업로드 크기 제한 |
| News Hub | 수집된 시장 뉴스를 읽고 중요한 소식은 알림으로 확인 | Investing.com·Google News RSS 수집, 기사 보관, 나스닥 관련성 분류, Telegram 알림, 수집 최신성 표시 |
| YouTube Memo | 영상을 저장하고 다시 볼 지점에 메모 작성 | 영상 링크 등록, 타임스탬프, 영상별 메모 |
| Book Memo | 책을 찾고 목차별로 읽은 내용 기록 | 책 검색, 내 책장, 목차 체크, 장별 코멘트 |
| 차량관리 | 차량 상태를 확인하고 정비 이력 기록 | Hyundai 연동, 주행거리·정비 주기 관리, 운행 종료 알림, Telegram 명령 |
| HomeOps | 서버 이상을 진단하고 허용된 범위에서 복구 | 상태 수집, 컨테이너 진단, 승인된 조치 실행, 실행 이력 |

프로젝트에서는 각 서비스의 기능뿐 아니라 인증, 데이터 저장, 장애 처리와 운영 절차까지 함께 구현했습니다. 특히 데이터를 잃거나 같은 작업을 두 번 처리하지 않도록 만드는 데 중점을 뒀습니다.

## 주요 화면

실제 서비스 화면입니다. 화면별 촬영 시점과 표시 범위는 [이미지 목록](docs/images/README.md)에 정리했습니다.

| Investing.com 뉴스 수집 | 차량 관리 Telegram |
|---|---|
| RSS 수집 결과, Telegram 알림 분류, 원문 이동이 가능한 기사 목록을 확인함.<br><br><img src="docs/images/news-hub.png" alt="Investing.com 뉴스 수집 결과와 기사 목록" width="460"> | 운행 기록과 누적 주행거리, 주행 가능 거리, 소모품 정비 시점을 Telegram으로 확인함.<br><br><img src="docs/images/car-care-telegram-status.png" alt="Telegram 차량 관리 운행 및 정비 상태" width="460"> |

| YouTube Memo | Book Memo |
|---|---|
| 영상 링크 등록과 저장된 영상별 메모 수를 확인함.<br><br><img src="docs/images/youtube-memo.png" alt="YouTube Memo 영상 등록과 저장된 영상 목록" width="460"> | 책 검색, 내 책장, 목차 체크, 장별 코멘트 기록을 한 화면에서 확인함.<br><br><img src="docs/images/book-memo.png" alt="Book Memo 책장과 목차 체크 및 코멘트 기능" width="460"> |

| 파일함 | 관리자 상태 |
|---|---|
| 파일·폴더 생성, 업로드, 다운로드, 검색, 정렬과 선택 삭제를 지원함.<br><br><img src="docs/images/file-manager.png" alt="파일함의 파일 목록과 업로드 및 검색 기능" width="460"> | 서버 자원, 서비스 Health, 승인형 HomeOps 조치 이력을 확인함.<br><br><img src="docs/images/admin-status.png" alt="관리자 상태와 HomeOps 운영 보조" width="360"> |


<br>

## 구조와 기술 선택

![Personal Server 현재 운영 구조](docs/images/personal-server-architecture-v2.svg)

Windows의 Ubuntu WSL2에서 공개 웹 서비스와 모니터링은 K3s로, 차량관리와 운영 보조 작업은 Docker Compose로 실행합니다. 외부 요청은 Cloudflare Tunnel과 Caddy를 거쳐 각 웹 서비스로 전달됩니다.

| 영역 | 기술 | 사용 방식 |
|---|---|---|
| 웹·API | Python, FastAPI | 서비스별 API와 요청 처리 |
| 화면 | Jinja2, HTML/CSS, JavaScript | 서버에서 화면을 렌더링하고 검색·갱신 등 필요한 동작을 JavaScript로 처리 |
| 저장 | SQLite, JSON 파일, 파일 시스템 | 서비스별 메모·상태·기사·업로드 파일 저장 |
| 실행 환경 | Windows, Ubuntu WSL2, Docker, K3s | 단일 N100에서 웹 앱과 운영 작업을 역할별로 분리 |
| 외부 접근 | Cloudflare Tunnel, Caddy | 공개 호스트별 요청을 내부 서비스로 연결 |
| 관측·알림 | Prometheus, Grafana, Telegram | 지표 수집, 대시보드, 장애·복구·업무 알림 |
| 검증·배포 | unittest, GitHub Actions, Trivy | 서비스별 테스트, 보안 검사, 변경 범위에 따른 배포 |

### 서비스와 저장소를 나눈 이유

뉴스 수집, 메모, 파일 관리는 실행 주기와 데이터 형태가 다릅니다. 각 서비스가 자신의 데이터를 관리하고 Portal은 검색 API로 결과를 모으도록 구성했습니다. 한 서비스가 응답하지 않아도 나머지 서비스를 사용할 수 있도록 요청 실패도 서비스별로 처리합니다.

개인용 데이터 규모에 맞춰 SQLite와 파일 저장을 사용합니다. 별도 데이터베이스 서버 운영 부담은 줄지만, 동시에 쓰는 작업과 배포 중 데이터 접근을 직접 관리해야 합니다. K3s 앱은 서비스별 PVC에 데이터를 보관하고, 같은 운영 데이터를 수정하는 실행 주체는 하나만 두는 방식으로 운영합니다.

### K3s와 Compose를 함께 쓰는 이유

웹 앱은 K3s의 Service, readiness 검사, PVC를 기준으로 운영합니다. 차량관리와 호스트에 가까운 운영 보조 작업은 Compose에 남겨 두었습니다. 두 환경이 함께 있으므로 배포·복구 스크립트가 서비스의 실제 실행 위치를 확인해야 합니다. K3s로 옮긴 서비스를 Docker에서 다시 시작하지 않도록 이 구분을 코드로 검사합니다.

#### Crawler Worker K3s 현재 운영 기준

뉴스를 실제로 수집하고 저장하는 실행 주체는 K3s `crawler-worker`입니다. 기존 Docker 컨테이너는 중지된 롤백 자산으로 보존하며 동시에 실행하지 않습니다. 실행 위치를 기록한 표시 파일, PVC, Deployment readiness를 함께 확인합니다. [전환·복구 기준](docs/operations-reference.md#뉴스-수집-k3s-현재-운영-기준)에 상세 절차를 정리했습니다.

## 개발하면서 해결한 문제

### 1. 검색 결과가 없는 것과 검색에 실패한 것을 구분

여러 서비스를 묶어 검색할 때 빈 목록만 반환하면, 일치하는 자료가 없는지 대상 서비스가 응답하지 않는지 알 수 없습니다.

전체 검색 응답을 서비스별 결과와 상태로 나눴습니다. 정상 응답에 검색 결과가 없으면 빈 목록을 표시하고, 요청 실패나 잘못된 응답에는 `현재 응답 없음`을 표시합니다. 한 서비스의 실패 때문에 다른 검색 결과가 사라지지 않도록 했습니다. 서비스가 반환한 상대 경로도 실제 공개 주소로 연결되도록 처리했습니다.

관련 구현: [통합 검색](portal-web/app/services/global_search.py), [Portal 회귀 테스트](tests/test_portal_dashboard.py)

### 2. 같은 이름의 동시 업로드가 파일을 덮어쓰는 문제

파일 존재 여부를 확인한 뒤 저장하는 방식은 확인과 생성 사이에 다른 요청이 들어올 수 있습니다. 실패한 요청의 정리 과정에서 다른 요청이 저장한 파일까지 삭제할 위험도 있습니다.

파일 생성 자체가 중복을 거부하도록 배타적 생성 모드(`xb`)를 사용했습니다. 같은 이름으로 업로드하면 파일을 먼저 생성한 요청만 진행하고, 나머지는 이미 존재하는 파일이라는 오류를 받습니다. 생성에 실패한 요청은 파일 삭제 단계에 들어가지 않도록 처리했습니다.

관련 구현: [파일 저장](portal-web/app/services/file_store.py), [파일 접근 테스트](tests/test_file_access.py)

### 3. 응답 전송 실패가 정비 이력 중복으로 이어지지 않도록 처리

Telegram 명령으로 정비 이력을 저장한 뒤 응답 전송에 실패하면, 같은 메시지가 다시 처리될 수 있습니다. 메모리에서만 처리 여부를 기억하면 재시작 뒤에는 중복을 구분할 수 없습니다.

차량 명령의 메시지 식별자(`update_id`)와 처리 결과를 SQLite에 저장하고, 업무 데이터 변경과 처리 기록을 같은 트랜잭션으로 묶었습니다. 같은 명령이 다시 들어오면 저장한 결과를 사용해 정비 이력을 다시 만들지 않도록 했습니다.

관련 구현: [차량 데이터 저장](car-care-worker/app/services/store.py), [Telegram 명령 처리](car-care-worker/app/services/telegram.py)

### 4. 예전 기사가 보인다고 수집기가 정상인 것은 아니라는 점

뉴스 화면에 저장된 기사가 계속 표시되면 수집이 멈춰도 알아차리기 어렵습니다. 기사 목록과 별도로 `마지막 정상 수집` 시각, 마지막 시도, 연속 실패 횟수를 기록해 수집 최신성을 확인할 수 있게 했습니다. 정상 수집 기록이 없는 초기 상태도 구분합니다.

상태 API는 수집 상태에 필요한 항목만 반환합니다. 기사 내용이나 자격 증명은 포함하지 않습니다. 알림은 모든 수집 기사를 보내는 대신 관련성 분류와 알림 판단을 나눠, 전망성 기사와 확정된 시장 충격 기사를 다르게 처리했습니다.

관련 구현: [수집 상태 관리](crawler-worker/app/services/news_collection_status.py), [기사 분류](crawler-worker/app/crawlers/news_quality.py)

### 5. 자동복구가 다른 실행 환경의 서비스를 건드리지 않도록 제한

K3s 전환 뒤에도 예전 Docker 컨테이너가 남아 있으면, 복구 도구가 이를 다시 실행해 같은 데이터를 두 곳에서 수정할 수 있습니다.

배포와 복구 전에 서비스별 실행 위치를 검사하고 K3s 소유 서비스는 Docker 동작에서 제외합니다. 안전 배포에서는 표시 파일이 없거나 값이 누락·중복되거나 신뢰할 수 없을 때 작업을 중단합니다. HomeOps에도 허용 서비스, 승인 확인, 실행 간격과 횟수 제한을 두고 조치 이력을 남깁니다.

관련 구현: [실행 위치 검사](scripts/runtime-service-state-reader.py), [HomeOps Docker 작업](homeops-executor/app/services/docker_ops.py), [실행 위치 계약 테스트](tests/test_runtime_service_state.py)

## 운영과 데이터 보호

서버 내부 점검과 외부 접속 확인을 분리했습니다. 내부 지표만으로는 공개 경로의 장애를 놓칠 수 있어 GitHub Actions에서도 약 5분 간격으로 외부 health를 확인합니다. 장애와 복구 상태의 변화는 Telegram으로 전달합니다.

| 항목 | 적용 내용 |
|---|---|
| 상태 확인 | Prometheus·Grafana 지표와 관리자 화면, 외부 health 점검 |
| 제한형 복구 | HomeOps 조치와 N100 제한형 자동복구에 대상·실행 조건·횟수 제한 적용 |
| 데이터 보존 | 앱 데이터는 PVC에 유지하고 이미지 교체와 데이터 변경을 별도 절차로 처리 |
| 백업 | Portal PVC 암호화 백업 및 실제 복원 검증 |
| 인증 | 관리자·파일함·메모 쓰기 권한 분리, 세션 인증과 Origin 검증 |
| 요청 보호 | HttpOnly·SameSite 쿠키, 보안 헤더, 재시작 후에도 유지되는 인증 실패 제한 |
| 컨테이너·공급망 | non-root 실행 사용자 설정, 베이스 이미지 digest와 외부 Action SHA 고정, Trivy 검사 |

### 모니터링 화면

아래는 N100에서 촬영한 Grafana 화면입니다. WSL2 환경에 맞춰 node-exporter를 비활성화한 구성이라 상단의 일부 CPU·메모리 사용률 패널은 `No data`로 표시됩니다. 조회 가능한 그래프와 누락된 지표를 구분해 사용하고 있습니다.

![Grafana K3s 모니터링 화면](docs/images/grafana-k3s-overview.png)

## 검증

서비스마다 의존성과 Python 버전이 다르므로 테스트 실행 환경을 분리했습니다. 로컬 실행기와 CI가 같은 테스트 목록을 사용하고, 테스트 파일이 목록에서 누락됐는지도 검사합니다.

정상 요청뿐 아니라 인증 실패, 잘못된 입력, 동시 저장, 부분 장애, 재시작 뒤 재처리와 같은 경계를 회귀 테스트로 확인합니다. 운영 스크립트는 대체 명령을 사용한 계약 테스트로 검사하고, 실제 배포 후 health와 데이터 검증은 별도로 기록합니다.

로컬 테스트 환경을 준비한 뒤 다음 명령으로 실행할 수 있습니다. 환경 구성은 [개발 환경 안내](docs/agent-handoff.md)를 참고합니다.

```bash
python3 tests/run_service_tests.py
python3 -m unittest tests.test_documentation_index -v
```

### 배포 흐름

기능 브랜치에서 변경하고 테스트와 검토를 거쳐 PR을 `main`에 병합합니다. 병합 후에는 CI 결과와 배포 분류를 확인합니다. Compose 안전 배포는 허용된 서비스만 대상으로 하며, K3s에서 실행 중인 서비스는 제외합니다. K3s 앱 이미지는 별도 교체 절차를 사용합니다.

코드가 병합됐다는 사실과 운영 서버에 적용됐다는 사실은 구분합니다. 뉴스 알림 영속화는 복구 호환 이미지를 거쳐 운영에 적용했고, 일일 SLO 수집도 수동 검증 뒤 자동 실행을 활성화했습니다. 각 적용 결과는 [뉴스·SLO 운영 적용 검증 결과](docs/reviews/20260922_뉴스_SLO_운영적용_검증결과.md)에 기록했습니다.

CI 산출물과 운영 증적은 90일 보관하며, 장기 보관이 필요한 자료는 별도 증적 저장소로 이전합니다.

## 현재 한계와 다음 작업

단일 N100과 WSL2에서 운영하므로 호스트 자체의 장애까지 견디는 고가용성 구성은 아닙니다. SQLite·파일 기반 저장과 단일 쓰기 프로세스를 전제로 하며, 다중 쓰기 인스턴스로 확장하려면 저장 구조를 다시 검토해야 합니다.

현재 남은 운영 검증은 다음 승인된 유지보수 재부팅에서 Windows BootTrigger 경로를 확인하는 일입니다. 일일 SLO 증적은 운영 중이며 2026-09-23 02:15 실행이 성공했습니다. 30일 SLO 목표값은 기준선 수집 뒤 재검토합니다. 코드·배포 상태와 검증 이력은 [개발 계획](docs/20260921_프로젝트보완_개발계획.md), [뉴스·SLO 운영 적용 검증 결과](docs/reviews/20260922_뉴스_SLO_운영적용_검증결과.md), [SLO 기준](docs/slo-baseline.md)에 기록하고 있습니다.

## 빠른 상태 확인

아래 명령은 구성이 완료된 N100의 WSL에서 사용하는 점검 명령입니다. 최초 설치는 [운영 환경 구성 문서](docs/n100-mt4-setup.md)를 참고합니다.

```bash
cd /mnt/c/personal-server
curl --fail --silent --show-error https://len.pe.kr/health
sudo k3s kubectl -n personal-server get deploy,pod,pvc
bash infra/k8s/tools/sre-health-audit.sh
```

## 운영 문서

| 문서 | 내용 |
|---|---|
| [문서 색인](docs/README.md) | 운영·설계·검증 문서 안내 |
| [운영 참조](docs/operations-reference.md) | 서비스 구조, 공개 경로, 일상 점검 |
| [N100 운영 환경](docs/n100-mt4-setup.md) | Windows·WSL2 시작과 장애 대응 |
| [안전 자동 배포](docs/n100-github-auto-deploy.md) | Compose 배포 대상과 검증·롤백 절차 |
| [K3s 운영](infra/k8s/README.md) | 앱 전환, 모니터링, PVC 백업 |
| [외부 상태 점검](docs/public-uptime-monitor.md) | 공개 경로의 장애·복구 알림 |
