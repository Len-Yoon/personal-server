# 운영 참조

현재 N100 개인 서버의 서비스 구조와 일상 점검 기준 문서임. 이 서버는 서비스 실행뿐 아니라 관측성, 장애 감지, 복구, 백업·복원 검증, 알림을 연결한 SRE 운영 체계로 관리함. 비밀값은 `.env`, Kubernetes Secret, GitHub Secret에만 보관하며 이 문서·명령 출력에 기록하지 않음.

## 현재 런타임

**Cloudflare Tunnel → Caddy → K3s Portal·Crawler Worker·Book Memo·YouTube Memo** 경로로 공개 서비스를 제공함. 차량 OAuth callback만 별도 비공개 upstream을 사용함. Caddy는 Portal·Crawler Worker·Book Memo·YouTube Memo의 K3s Service로 전달함.

| 구성 | 실행 위치 | 상태 데이터 |
|---|---|---|
| `portal-web` | K3s `personal-server` namespace | `portal-web-files-dynamic`, `portal-web-state-dynamic` PVC |
| `crawler-worker` | K3s `personal-server` namespace | `crawler-worker-data` PVC |
| `youtube-memo` | K3s `personal-server` namespace | `youtube-memo-data` PVC |
| `book-memo` | K3s `personal-server` namespace | `book-memo-data` PVC |
| `system-agent`, `homeops-executor` | Docker Compose 내부 경계 | 호스트 상태·제한형 운영 작업 |
| `car-care-worker` | Docker Compose | 차량관리 데이터·Telegram |
| Prometheus·Grafana·Alertmanager relay | K3s `monitoring` namespace | monitoring PVC와 Kubernetes Secret |

## 컨테이너 실행 권한

저장소의 앱·운영 도구 Dockerfile 12개는 모두 `USER 10001:10001`을 지정함. 실제 실행 이미지와 데이터 접근 권한은 서비스별 배포 증적으로 확인함. Caddy는 내부 80·443 포트 binding에 필요한 `NET_BIND_SERVICE` capability만 사용함.

Portal PVC는 `portal-web-files-dynamic`, `portal-web-state-dynamic` 두 개이며 K3s 단일 writer만 연결함. Portal non-root 이미지 교체 전에는 두 PVC의 UID/GID `10001:10001` 읽기·쓰기·디렉터리 접근 권한을 확인함. 권한 불충족 시 Deployment를 변경하지 않으며, PVC 권한 정렬은 자동화하지 않고 별도 운영 승인 아래 최소 범위로 수행함.

## 공개 도메인

| 도메인 | 현재 대상 |
|---|---|
| `len.pe.kr`, `portal.len.pe.kr`, `file.len.pe.kr`, `admin.len.pe.kr`, `portfolio.len.pe.kr` | K3s `portal-web` |
| `news.len.pe.kr` | Cloudflare Tunnel → Caddy → K3s `crawler-worker` Service |
| `memo.len.pe.kr` | Cloudflare Tunnel → Caddy → K3s `youtube-memo` Service |
| `books.len.pe.kr` | Cloudflare Tunnel → Caddy → K3s `book-memo` Service |
| `car.len.pe.kr` | `car-care-worker` OAuth callback. Caddy가 아닌 별도 Cloudflare Tunnel ingress의 비공개 callback upstream |

## 운영 상태 확인 기록

운영 판정은 현재 Job·원본 백업/복원 증적·만료·공유잠금·단일 writer·Ready와 외부 health를 함께 대조함. 코드 병합·Job 종료·업로드 성공만으로 복원 가능성과 알림 수신을 확정하지 않음. 백업 생성 시각과 상태 조회 시각을 구분하며 다음 정기 실행까지 유효하지 않은 이전 증거는 재사용하지 않음.

[백업 운영 기준](#차량-db-백업과-자동-보관)과 [현재 확인 필요 사항](20260921_프로젝트보완_개발계획.md#확인-필요-사항)을 기준으로 점검함.

## 일상 상태 확인

N100 WSL에서 실행함.

```bash
cd /mnt/c/personal-server
curl --fail --silent --show-error https://len.pe.kr/health
sudo k3s kubectl -n personal-server get deploy,pod,pvc
bash infra/k8s/tools/sre-health-audit.sh
```

Grafana, Prometheus, Telegram relay, Portal PVC 백업은 [K3s 운영 문서](../infra/k8s/README.md)를 따름.

Loki 로그 관측 실습은 `observability-lab` namespace의 전용 자원과 `monitoring`의 전용 Grafana ConfigMap 2개만 대상으로 함. 적용·검증·명시적 롤백 명령 및 PVC 보존 기준은 [K3s 운영 문서의 Loki 실습 절](../infra/k8s/README.md#loki-로그-관측-실습)을 따름. [운영 검증 결과](reviews/20261004_Loki_Ansible_실습_N100운영검증결과.md)에 최초 조회 실패와 미검증 항목을 분리 기록함.

## 차량 DB 백업과 자동 보관

현재 적용된 백업·보관·감시 기준임. [현재 검증 상태](20260921_프로젝트보완_개발계획.md)와 [차량 자동화 상세 안내](../car-care-worker/tools/automation/README.md)를 참조함.

| 항목 | 현재 적용 기준 | 비고 |
|---|---|---|
| 일정 | 매일02:30 KST Portal→Book→YouTube→Crawler→차량 DB | 별도05:30 차량 timer는disabled/inactive. 기존 네 단계 모두 성공한 뒤 차량 호출 |
| 대상 | 차량관리 SQLite DB만 기존 전용 원격 폴더에 암호화 보관 | OAuth·자격 증명 데이터는 백업 대상 제외 |
| 변경 없음 | 논리 내용이 같으면 업로드·원격 정리 생략 | 일요일에는 실제 업로드·재다운로드·격리 복원 검증 |
| 원격 보관 | 일간7·주간4·월간3 구간별 최신본과 전체 최신7개 안전 하한 | 같은 초 동률 보존. 새 백업·복원 성공 뒤 정확 계획/현재 inventory/암호문 digest/최신 실복원 검증 후 삭제 |
| 실행 기록 | 30일 지난 성공 Pod·완료journal 쌍만 검사당 최대1개 정리 | 최신·실패·불확실 기록, 백업/보관 증적·삭제 기록 보존 |
| 누락 감지 | 서버user timer5분,09:00 이후 당일 미완료 통보 | 백업 재실행 없음. 최초02:30 이후 활성화한 날은 다음날부터 판정 |
| 상태 표시 | 인증 관리자 화면에서 차량 DB 원본 증적·최근 실행·정리 결과 표시 | 복원 증거는원본시각+8일. 관측·변경 없음 시각으로 유효 기간 연장하지 않음 |
| 잠금 | 기존 공유잠금 사용, 생산 중 모니터 게시·알림·정리 유예 | 잠금이 비정상적으로 계속 점유되면 누락 알림도 지연될 수 있음 |
| 실제 확인 | 원격 암호문 digest·최신본 복원·인증 화면/API 정상·비인증 상세 숨김 | Portal 외부·Relay 내부 health각10초 간격3회 정상 |
| 정기 실행 확인 | 2026-10-19 02:30~02:40 다섯 단계 자동 실행·당일 백업/격리 복원 성공 | 차량 신규 업로드·복원 성공이며 무변경 skip 아님. 잠금 해제·단일 writer·네 앱 외부 health/readiness 3회 정상 |
| 확인 필요 | 차량 원격 보관 실제 삭제·30일 성공 기록 정리·Telegram 단말 수신 | 차량 이번 정리는 no_op·삭제 0·보존 7. 네 앱 원격 사본은 각 1개 자동 삭제 확인 |

## 저장소 개선의 적용·검증 기준

다음 기능은 운영 이미지·소스·설정에 반영됨. [개발·운영 현재 상태](20260921_프로젝트보완_개발계획.md#추가-개선의-저장소운영-경계)의 검증 경계를 따르며, 신규 백업 이미지 적용 후 첫 정기 실행은 2026-10-19 당일 백업·복원 증적으로 확인했으며 실제 알림 수신은 별도 확인 대상임.

### 포트폴리오 동시 편집

편집 화면의 원본 내용 버전을 저장 요청에 포함하고 프로세스 간 잠금 안에서 현재 버전과 비교함. 오래된 요청은 409로 거절하고 작성 내용·서버 최신 내용을 비교할 수 있게 유지함. 최신 버전으로 전환하려면 사용자가 비교 버튼을 명시적으로 선택해야 하며 자동 덮어쓰기는 하지 않음. 로그인·초안 복구에도 원래 버전을 보존하고 구형 초안의 버전을 임의로 최신 값에 연결하지 않음. HTML 폼 줄바꿈은 LF로 정규화함. 같은 내용의 응답 유실 재전송은 재쓰기 없이 성공으로 처리함. 2026-10-18 사용자 승인으로 기존 Portal runtime Secret에 전용 `PORTFOLIO_ADMIN_PASSWORD`를 설정하고 외부 HTTPS 로그인·실제 저장·동일 요청 무재쓰기·과거 revision 409·시험 데이터 정리를 검증함. 전용 비밀번호는 Mac 로그인 키체인의 전용 항목에 보관하며 일반 파일·명령 인자·로그에 복제하지 않음. 공백 시험은 공개 안내를 유지했으며 일반 Markdown 제목·한글 문단의 실제 공개 표시도 별도로 확인함. 시험 생성 파일과 빈 폴더를 제거해 원래 부재 상태·기본 안내 화면으로 복원함.

### 준비 상태와 보관 용량

Book·YouTube 신규 기본 manifest와 생성 검증은 readiness `/ready`, liveness `/health`를 사용함. 이미 존재하는 비활성 원복 자산의 readiness `/health`는 호환 검사에서 허용하며 자동 변경하지 않음.

Prometheus의 실제 로드된 보관 설정은 `1w or 3GiB`이며 7일과 `retentionSize: 3GB` 중 먼저 도달하는 기준으로 보관함. 요청 용량 5Gi의 일부를 WAL·head·compaction에 남기지만 local-path PVC 요청 값은 호스트 디스크 강제 할당량이 아니며 정확히 7일 보존을 보장하지 않음. Loki 실습의 24시간 retention도 즉시 삭제·디스크 사용량 상한을 뜻하지 않음.

`NodeDiskPressure`는 DiskPressure 5분 지속, `PVCSpaceLow`는 사용 가능 공간 15% 미만 10분 지속 시 기존 `sre_telegram=true` 경로로 전달하도록 정의함. 규칙 정의와 실제 알림 수신은 별개임. `python3 infra/k8s/tools/monitoring-disk-audit.py`는 읽기 전용으로 Windows C:와 Bound PVC/노드 지표·전용 target·실제 로드된 규칙/식/기간/라벨을 대조함. 미제공은 GAP(exit 1), 읽기·증거 오류는 FAIL(exit 2)로 반환함. 현재 Windows C:·노드·PVC coverage는 PASS임. WSL root 전체의 실제 디스크·mount를 식별하는 직접 용량 감시는 이번 범위가 아니며 기존 노드/PVC 신호와 구분함. 읽기 전용 audit 자체는 알림을 보내지 않아 notification_delivery=NOT_TESTED를 유지하며, 아래 별도 시험의 실제 전송 증적 및 사용자 단말 수신과 구분함. 부분 지표나 수동 실측을 전체 호스트 coverage로 확대하지 않음.

### Windows 호스트 디스크 경보 운영

2026-10-19 읽기 전용 실측에서 Windows C: 공간은 정상이나 Prometheus 수집·경보 연결이 없는 공백을 확인함. WSL ext4의 노드/PVC 지표로 Windows C: 상태를 대신 판정하지 않음. 기존 Windows collector→host-metrics.json→system-agent 경로에 출처·고정 볼륨·총량/가용 bytes를 추가하고 기존 private bridge를 EndpointSlice ServiceMonitor로 수집함. 무효·누락·미래·노후 자료는 collection_ok=0으로 표시하고 원본 수집시각을 scrape 시각으로 덮어쓰지 않음.

전용 `HostDiskSpaceLow`는 fresh 원본의 C: 가용 15% 미만 10분, `HostDiskMetricsStale`은 자료 미제공/무효/35분 초과 노후가 5분 지속되면 기존 SRE 경로로 전달함. 원본 수집만 멈추는 경우 탐지까지 최대 약 40분과 평가·전송 지연이 걸리며, C: 이외 볼륨과 WSL root 전체의 직접 용량 감시는 이번 범위가 아님. 기존 노드/PVC 경보는 유지함. 전용 `HostDiskNotificationTest` 생성 도구는 실행 시점 기준 최대 5분 내 만료하는 시험 조건만 준비하며 Kubernetes·Telegram을 호출하지 않음. 실제 시험은 운영 승인 후 바로 생성·적용하고 발화·복구·정확한 시험 자원 제거를 확인함. 시험 메시지는 운영 장애가 아님을 고정 문구로 표시함. Telegram API 수락과 사용자 단말 실제 수신은 구분함.

2026-10-19 사용자 승인 후 검증된 소스·이미지, Windows child 수집기, 전용 ServiceMonitor·Rule을 운영 반영함. 정확한 한 target UP·fresh C: 지표와 호스트/노드/PVC 규칙 audit PASS를 확인함. 자동 만료 시험에서 발생·종료 Telegram API 수락과 Relay 원장 기록을 각각 확인한 뒤 정확한 UID/resourceVersion 조건으로 시험 Rule을 제거함. 사용자 단말의 실제 수신은 별도 확인 필요함. [개발 계획의 승인 범위와 성공 기준](20260921_프로젝트보완_개발계획.md#호스트-디스크-감시경보-전달-보완)을 따름. 기존 supervisor·예약 작업·백업 일정·자격 증명 Secret·PVC·Caddy를 보존함. 새 수집 설정에 따른 기존 Prometheus 소유 생성 설정 Secret만 Operator가 자동 갱신했으며 소유 CR·UID/type·생성 설정 키를 확인함.

### 백업 보관 정리 결과

Portal·Book·YouTube·Crawler·차량 DB의 관리자 표시는 백업·복원 원본 시각과 별도로 정리 시각·보존 수·확정 삭제 수·확정 회수 bytes·정리 사유를 표시함. 삭제 API 응답만으로 회수량을 확정하지 않으며 원격 목록에서 대상이 사라진 사실까지 확인한 항목만 집계함. 일부 삭제 후 실패하면 확인된 삭제량을 유지하고 보존 수는 확인 필요로 표시함. 구형 증적의 미제공 값은 0으로 바꾸지 않음.

사유는 증거 만료·불일치, 목록 변경·조회 실패, 삭제 실패·확인 불가 등 고정 코드로만 전달하며 경로·객체 ID·자격 증명·예외 원문을 API에 포함하지 않음. 정리 상태 조회가 복원 검증 시각이나 유효 기간을 연장하지 않음. 새 소비자인 Relay·Portal을 먼저 적용하고 기존 증적 호환을 확인한 뒤 생산자·monitor를 적용함. 네 CronJob의 이미지와 순차 실행기의 고정값은 일치하며 일정·suspend는 유지함. 차량은 기존 백업 실행 이미지·unit을 유지하고 검증된 host 소스만 사용함. 2026-10-19 정기 실행에서 네 앱의 당일 백업·복원과 정리 summary success·각 원격 사본 1개 삭제를 확인함. Portal 보존 12·회수 67,047,592byte, Book 보존 8·737,656byte, YouTube 보존 8·420,136byte, Crawler 보존 8·14,851,816byte임. 차량은 당일 신규 백업·복원 성공이며 보관 정리는 no_op(삭제 0·보존 7·회수 0byte)임. 차량의 기간 경과 실제 삭제·30일 성공 기록 정리까지 완료했다고 확대하지 않으며 수동 백업·삭제는 실행하지 않음.

## 장애 알림과 복구

| 신호 | 감지 방식 | 알림 |
|---|---|---|
| 공개 주소 장애 | GitHub Actions 약 5분 간격 health 점검 | Telegram 장애·복구 전환 시 1회. N100 알림과 중복 가능 |
| N100 기반 구성요소 이상 | `personal-server-autostart`의 Supervisor가 Daemon을 단일 관리하고, Daemon이 3분 간격으로 WSL·K3s·Portal·NodePort·Tunnel 점검 | Tunnel 장애 알림 전송 성공 뒤 정상 복구 전환 시 Telegram 각 1회 |
| K3s·노드·워크로드 이상 | Prometheus Alertmanager → SRE relay | Telegram |
| Portal PVC 백업·복원 검증 | 02:30 순차 실행기의 소유 K3s Job·증적·순차 상태 | Telegram SRE relay |
| 차량 DB 백업 실패·누락 | 차량 완료journal·복원 증적 및5분 모니터 | 기존 Alertmanager→SRE relay. 실제 단말 수신은 별도 확인 |
| Compose 컨테이너 이상 | HomeOps 진단·제한형 복구 | 관리자 상태·필요 시 Telegram |
| 뉴스 수집 지연·연속 실패 | Prometheus `NewsCollectionStale` | SRE relay → Telegram |

N100 자동복구는 Windows 시작 시 Supervisor를 하나만 실행하고, 초기 120초 뒤 Daemon을 하나만 시작함. Daemon이 비정상 종료되면 Supervisor가 15초 뒤 재기동하며, 60초 안에 3회 연속 종료되면 60초 backoff를 적용함. Tunnel은 NodePort·서비스·프로세스·공개 health를 함께 확인하고, NodePort 장애는 Tunnel 알림·복구로 오분류하지 않음. 같은 구성요소가 2회 연속 비정상일 때만 승인된 제한 복구를 시도하고, 구성요소별 시도 횟수를 최대 3회로 제한함. Supervisor의 host metrics 기록 실패는 감시 기동을 막지 않지만, 상태 기록을 저장하지 못하면 중복·무한 복구를 막기 위해 추가 복구를 중단함. K3s가 이미 실행 중이면 재시작하지 않으며, Portal PVC·Secret·운영 데이터·Caddy·Tunnel ingress는 수정하지 않음.

N100 감시기는 Tunnel 장애의 최초 전환에 직접 Telegram 장애 알림을 1회 시도함. 전송이 성공한 상태에서만 정상 복구 전환을 Telegram으로 1회 알리며, 전송 실패 시 다음 점검에서 장애 알림을 재시도함. GitHub Actions의 외부 점검은 독립 보완 경로이므로 같은 장애에 대한 Telegram 알림은 중복될 수 있음. N100 전원·네트워크·WSL 자체가 동작하지 않는 경우에는 N100 감시·직접 알림·자동복구를 보장할 수 없음. Telegram 자격 증명은 Windows 자격 증명 관리자에만 보관하며 문서·로그·상태 파일에는 기록하지 않음.

공개 상태 Telegram 알림의 Secret 설정과 수동 점검은 [공개 상태 Telegram 알림](public-uptime-monitor.md)을 따름.

HomeOps 실행기는 Docker socket을 제한된 allowlist 진단·재시작에만 사용함. Portal 관리자 비밀번호는 실행기 통신에 재사용하지 않으며, 운영자가 사전 설정한 `HOMEOPS_EXECUTOR_SHARED_SECRET`이 비어 있으면 Portal과 실행기 모두 HomeOps 요청을 fail-closed 처리함. 값은 Git·문서·로그·상태 파일에 기록하지 않음.

### HomeOps 공유 인증 확인·제한 구성

`infra/k8s/tools/homeops-auth-configure.py --check`는 기존 Portal Secret·양측 환경·인증된 진단을 읽기 전용으로 확인함. 미설정은 `NOT_CONFIGURED`, 실행기 재동기화 필요는 `EXECUTOR_SYNC_REQUIRED`로 구분함. 실제 구성은 대상·동작에 대한 운영 승인과 PR CI를 통과한 정확한 커밋을 확인한 N100에서만 수행함.

```bash
python3 infra/k8s/tools/homeops-auth-configure.py --check
# 아래 명령은 별도 승인된 운영 적용 단계에서만 실행함.
python3 infra/k8s/tools/homeops-auth-configure.py --go --expected-commit "$(git rev-parse HEAD)"
```

기존 Portal Secret을 인증 원본으로 사용하며 다른 키를 보존함. 실행기 기존 이미지·Compose 파일·네트워크·환경·관리 대상 3개와 Portal 단일 writer를 대조함. 실패 시 이번 작업의 변경만 조건부 원복하며, 원복 중 동시 변경을 발견하면 덮어쓰지 않고 수동 확인을 요구함. 진단은 읽기 전용이며 관리 재시작 POST는 검증에 사용하지 않음.

일반 Docker restart에는 인증 환경값이 유지됨. 후속 승인으로 `scripts/homeops-auth-run.py`를 `windows-bootstrap.sh` 및 `deploy-n100.sh`의 `k3s`·`cutover` 실행기 생성 경로에 연결함. 기존 Portal Secret의 필수 참조·키를 조회하고 Compose 자식 환경에만 공급함. Portal Ready는 요구하지 않지만 Kubernetes API 조회는 필요함. 조회 실패·인증 누락·잘못된 참조·상속 환경의 인증 불일치 시 생성 전에 중단함. 키 생성·회전·Secret 수정은 수행하지 않음.

지원 명령은 `config --quiet` 및 명시된 관리 대상의 `up`으로 제한함. 비밀값을 출력할 수 있는 `config` 렌더링·`exec` 등은 허용하지 않으며 자식 출력도 캡처하여 고정 결과 코드만 표시함. 실패 시 Secret 참조와 읽기 권한을 확인한 뒤 동일 승인 범위에서 재실행함. 도구가 새 권한을 생성하거나 자동 재시도하지 않음.

직접 실행하는 일반 `docker compose`, legacy `compose` 모드, `portal-cutover.sh`의 writer 전환은 자동 공급 대상이 아님. 해당 경로는 별도 운영 절차·승인을 확인해야 함. 전체 bootstrap·공식 수동 배포는 기존 이미지 선택·다른 서비스 재생성 동작도 포함하므로 실행기 단독 검증 명령으로 사용하지 않음. 실서버 검증은 기존 이미지·Compose 파일·프로젝트를 보존한 실행기 1개 재생성으로 한정함. 실제 호스트 재부팅은 미수행임. Kubernetes Secret 암호화·접근 정책의 추가 검증은 별도 작업임.

## 운영 경계

[자동 재공급 운영 검증](reviews/20261006_기능실패경계_N100운영적용결과.md#8-homeops-인증-자동-재공급): 부모 인증값 없이 기존 이미지의 실행기 1개를 재생성하여 실제 진단·403 거부·보호 상태·외부 health 15/15를 확인함. Secret·Portal은 변경하지 않았으며 실제 호스트 재부팅 검증을 대체하지 않음.

- Portal은 K3s 단일 writer로만 실행함. Compose Portal과 동시에 실행하지 않음.
- K3s Portal 전환·rollback·PVC 작업은 `infra/k8s/tools/portal-cutover.sh`의 명시적 운영 절차만 사용함.
- 자동 배포는 `crawler-worker`, `youtube-memo`, `book-memo`, `car-care-worker`의 허용된 변경을 분류함. 현재 K3s runtime state의 `crawler-worker`, `book-memo`, `youtube-memo`는 Compose 안전 배포에서 `safe_cd_skip_k3s_service`로 생략되며, 실제 Compose 배포 대상은 `car-care-worker`임.
- Caddy, Cloudflare Tunnel, K3s Secret·PVC, `.env`, `data/`, Portal은 자동 배포에서 제외함.
- Trivy filesystem/config 검사는 HIGH·CRITICAL 결과를 CI 차단 기준으로 사용함.

## K3s 애플리케이션 이미지 교체 절차

K3s 애플리케이션의 런타임 이미지만 교체하는 운영 절차임. 실제 적용은 서비스별 별도 승인 후 운영자가 수행하며, 저장소 변경 또는 CI 통과를 운영 적용 완료로 간주하지 않음. 검증된 이미지 digest·소스 커밋·archive 체크섬은 배포 증적에 기록할 수 있음. Secret 값·내부 IP·개인 접속 경로는 기록하지 않음. 현재 공식 upgrade 도구 지원 대상은 `portal-web`, `crawler-worker`, `book-memo`임. YouTube는 이 도구의 지원 대상이 아니며, [검증된 이미지 단일 필드 조건부 변경 절차](reviews/20260930_뉴스보관함_메모내보내기_N100운영반영결과.md)를 따른 사례가 있음.

1. macOS에서 `k3s-app-image-build.sh`로 대상 애플리케이션의 Linux AMD64 OCI archive를 생성함. `latest`가 아닌 immutable tag만 사용하고 archive SHA-256을 확인함. 2. N100에서 대상 애플리케이션명을 명시한 별도 운영 승인을 받은 후에만 `k3s-app-image-import.sh --go`로 archive와 SHA-256을 검증하여 반입함. importer가 출력한 canonical immutable digest 별칭만 이후 단계의 이미지 입력으로 사용함. 3. `k3s-app-upgrade.sh --check`으로 Deployment readiness, Service·Endpoint, 반입된 Linux AMD64 digest 이미지와 현재 애플리케이션 health를 읽기 전용으로 확인함. 4. `k3s-app-upgrade.sh --go`는 check 성공 이후에도 대상 애플리케이션명을 명시한 별도 운영 승인을 받은 경우에만 실행함. 실행 시 현재 immutable digest를 `--expected-current-image`으로 지정하여 변경 전 이미지가 예상과 일치할 때만 patch함. 5. rollout과 대상 애플리케이션 health를 확인한 뒤 다시 `--check`으로 결과를 검증함. Portal 교체는 Pod 내부 health와 공개 Portal health를 10초 간격으로 3회 모두 HTTP 200으로 확인함.

대상 rollout 또는 health 검증 실패 시 도구는 직전 immutable digest로 **1회만** rollback을 시도함. 이는 완료된 전환 이력이며 새 이미지의 rollback 호환성을 자동 보장하지 않음. rollback 실패·상태 불확실·두 번째 시도 필요 상황에서는 자동 재실행하지 않고 운영자가 상태를 확인한 뒤 별도 판단함. 이 절차는 Deployment의 컨테이너 이미지 외에는 변경하지 않으며 PVC, Secret, Caddy, Cloudflare Tunnel, Docker Compose를 생성·수정·삭제·재기동하지 않음.

### 추가형 DB 변경·Compose 이미지 교체 시 확인 기준

- Book·YouTube의 `version`·`write_requests`, 차량의 `unknown_warnings_json`은 앱 초기화의 추가형 변경임. 교체 전에 SQLite online backup·quick_check·기존 컬럼의 행 수/내용 해시와 이전 이미지를 확보함. 이미지 원복은 새 컬럼·테이블 삭제나 DB 백업 덮어쓰기를 포함하지 않음.
- Windows bind에서는 0700/0600 지정이 실제 제한 권한으로 적용되지 않을 수 있음. 기존 운영 데이터 권한을 임의로 변경하지 않고, 검증된 tmpfs snapshot과 WSL 접근 제한 저장소 등 실제 제한 권한을 확인한 위치에 복원점을 보존함. 인증·OAuth 파일을 복제하지 않음.
- HomeOps·차량은 기존 Compose 파일·환경 값·권한·마운트·네트워크·port binding을 대조하고 대상만 교체함. HomeOps의 현재 관리 집합을 기본값으로 넓히지 않음. bridge 주소 변수의 기본값과 실제 기존 바인딩이 같다고 가정하지 않으며, Secret·내부 주소를 검사 로그나 신규 파일에 기록하지 않음.
- Docker ImageId는 저장 엔진에 따라 config digest와 다를 수 있음. archive 체크섬과 실제 manifest/config·RootFS 대조로 확인한 런타임 ID를 사용함. 배열 순서가 무의미한 환경·마운트 비교는 실제 값·중복 환경 이름을 검사한 뒤 정규화함.

후속 작업은 해당 시점의 증적을 현재 실행 상태로 가정하지 않고 다시 읽기 전용 점검함.

## Book Memo K3s 현재 운영 기준

- `books.len.pe.kr`의 확정 경로는 Cloudflare Tunnel → Caddy → K3s `book-memo` Service이며, K3s Pod만 production writer로 사용함. 중지된 Docker `book-memo`와 동시에 production write를 허용하지 않음.
- `/var/lib/personal-server/k3s-runtime-services.state`의 root 소유 runtime state marker에 `book-memo=k3s`를 유지함. 이 marker가 없거나 신뢰할 수 없으면 자동화가 Compose 기본값으로 해석할 수 있으므로, 배포·점검 전 root 소유·일반 파일·비쓰기 가능 권한과 값을 확인함.
- K3s Service의 정적 ClusterIP는 Git에 기록하지 않음. 코드와 운영 문서는 Service 이름·port 계약만 사용하며, 실제 주소는 실행 시 Kubernetes API로 조회함.
- `data/book-memo`, `data/book-memo.final-docker-*`, `data/book-memo.pre-k3s-*`는 롤백·증적 자산으로 보존함. 일상 정리·자동 배포·이미지 정리에서 삭제하지 않음.
- 정적 `infra/k8s/apps/book-memo.yaml`은 초기 준비용 sentinel manifest임. 현재 production Deployment·Service·PVC에 정적 `book-memo.yaml`을 재적용하지 않음.
- Docker Compose의 `book-memo` 서비스 정의는 검증된 롤백 자산으로 유지함. Caddy는 중지된 Docker rollback 컨테이너의 healthy 상태를 기다리지 않도록 `book-memo` Compose `depends_on`을 사용하지 않음.

## Book Memo K3s 전환 승인·검증

다음 절차는 아직 전환하지 않은 환경에서만 사용하는 과거 전환 절차임. 현재 K3s production writer를 다시 전환하거나 정적 `book-memo.yaml`을 재적용하는 데 사용하지 않음. Book Memo 전환 도구는 서비스별 사용자 승인 후 운영자가 수동으로 실행함. 정적 `book-memo.yaml`은 실행 불가 sentinel 이미지와 replica 0만 포함하므로 직접 적용하지 않음. 운영 적용·공개 경로 변경·자동 실행 활성화를 저장소 구현 완료와 구분하여 보고함.

준비·전환 순서는 다음과 같음.

1. 검증된 Linux AMD64 OCI 이미지를 `k3s-app-image-import.sh --go`로 반입하고, 성공 출력의 canonical digest 별칭을 다음 단계 입력으로 사용함. 2. 운영자가 기존 런타임 설정과 동일한 `book-memo-runtime` Secret을 사전 시딩함. Secret 값은 조회·출력·복제하지 않음. 3. 별도 승인 후 `book-memo-prepare.sh --go --image docker.io/library/personal-server-book-memo@sha256:<digest>`를 실행함. 이 도구만 importer가 등록·검증한 canonical immutable digest 별칭을 sentinel에 메모리에서 치환하고 PVC·Deployment·Service를 replica 0으로 생성함. 이미 정확히 준비된 세 리소스가 있는 경우에는 `--bind-existing --image ...`만 사용하여 리소스 생성·수정 없이 PVC 바인딩만 수행함. 두 모드는 함께 지정할 수 없음. `WaitForFirstConsumer` StorageClass에서는 PVC 바인딩만을 위해 동일 digest 이미지의 임시 비작성 Pod를 생성함. 이 Pod는 서비스 계정 토큰·환경 변수·Secret을 사용하지 않고, read-only PVC만 mount한 상태로 짧게 대기한 뒤 소유 label과 UID를 재확인해 삭제함. 앱 writer 기동·원본 데이터 읽기·데이터 복사는 수행하지 않음. 4. `book-memo-cutover.sh --check`으로 writer 부재·PVC·이미지·Deployment 계약을 읽기 전용 검증함. 5. 별도 전환 승인 후에만 `book-memo-cutover.sh --go`를 실행함.

준비 도구는 Docker·원본 데이터·Secret 값을 읽거나 변경하지 않으며 replica를 1로 올리지 않음. 임시 Pod는 server dry-run 결과에서 단일 컨테이너·init/ephemeral container 없음·lifecycle 없음·비작성 보안 계약을 먼저 검증한 뒤에만 실제 생성함. 생성 응답이 불확실하면 이름·소유 label·UID를 다시 확인한 경우에만 UID 사전조건 삭제를 시도하며, 그렇지 않으면 다른 Pod를 삭제하지 않고 복구 필요 상태로 중단함. 임시 PVC 바인딩 Pod가 Ready 또는 PVC Bound가 되지 않거나 소유권·UID가 달라지면 기존 PVC·Deployment·Service를 삭제하지 않고 중단함. `--go`는 기존 PVC·Deployment·Service 중 하나라도 있으면 중단하고, `--bind-existing`은 세 리소스의 안전 계약·replica 0·writer Pod 부재를 모두 확인한 경우에만 수행함.

| 모드 | 승인·수행 범위 | 검증·실패 처리 | 비고 |
|---|---|---|---|
| `--check` | 읽기 전용 사전 점검 | Compose health, K3s writer 부재, 반입된 digest 고정 AMD64 이미지, Bound PVC, Deployment 계약 확인 | 데이터 복사 없음 |
| `--prepare` | 사전 점검 및 Kubernetes server dry-run | 실제 리소스를 생성하지 않음 | `book-memo-prepare.sh --go` 적용 이후에만 사용 |
| `--go` | 별도 전환 승인 후 Compose 중지, 빈 PVC로 1회 복사, K3s writer 기동 | Compose 중지 재확인, 양쪽 SQLite `PRAGMA quick_check`, 전체 데이터 digest 일치, rollout 확인 | 실패 시 K3s 종료 확인 후 Compose 복구 |
| `--rollback` | 별도 롤백 승인 후 K3s 중지 및 Compose 복귀 | 양쪽 데이터가 같을 때만 복귀함. 데이터가 달라졌으면 K3s를 복구하고 실패함 | 신규 쓰기 이후 역방향 데이터 복사는 별도 승인 필요 |

네 가지 모드는 상호 배타적이며 중복 지정도 거부함. `--go`만 정방향 전환을 승인하며 `--rollback`은 독립적인 롤백 승인임. 모든 모드에서 `--source`, `--database`, `--image` 입력을 검증하며, 실제 데이터 위치와 이미지 digest 값은 문서·Git·로그에 기록하지 않음. Secret 키 이름은 `BOOK_MEMO_DB_PATH`, `ALADIN_TTB_KEY`, `DELETE_PASSWORD`, `APP_ENV`, `AUTH_RATE_LIMIT_STATE_PATH`이며 값은 사전 시딩 절차로만 관리함. 운영자는 DB 설정과 검증 대상의 일치 여부 및 runtime 쓰기 권한을 별도 확인함.

전환·롤백 동안 자동 복구·배포·다른 운영자의 writer 재기동이 개입하지 않는 유지보수 조건을 사전에 확인함. 전환 도구는 임시 데이터 검증 Pod만 생성·삭제하며 Secret을 조회·변경하지 않음. 복사 대상이 비어 있지 않으면 덮어쓰지 않음. 실패한 복사의 잔여 데이터는 보존하며 재시도 전 별도 검토가 필요함. `recovery_required` 실패 단계가 보고되면 자동 재실행하지 않고 writer 상태와 복구 가능 여부를 확인함.

공개 경로는 별도 승인·검증 대상이며 이 도구의 성공은 공개 서비스 전환 완료를 의미하지 않음. Secret·Portal·Caddy·Tunnel은 변경하지 않음. 적용 전후 독립 운영 검토, 서비스 수동 검증, 외부 health 검증은 실제 적용 승인 후 별도로 수행함.

## YouTube Memo K3s 현재 운영 기준

- `memo.len.pe.kr`의 확정 경로는 Cloudflare Tunnel → Caddy → K3s `youtube-memo` Service이며, K3s Pod만 production writer로 사용함. 중지된 Docker `youtube-memo`와 동시에 production write를 허용하지 않음.
- `/var/lib/personal-server/k3s-runtime-services.state`의 root 소유 runtime state marker에 `youtube-memo=k3s`를 유지함. 이 marker가 없거나 신뢰할 수 없으면 자동화가 Compose 기본값으로 해석할 수 있으므로, 배포·점검 전 root 소유·일반 파일·비쓰기 가능 권한과 값을 확인함.
- K3s Service의 정적 ClusterIP는 Git에 기록하지 않음. 코드와 운영 문서는 Service 이름·port 계약만 사용하며, 실제 주소는 실행 시 Kubernetes API로 조회함.
- Docker 원본 `data/youtube-memo`는 롤백·증적 자산으로 보존함. 일상 정리·자동 배포·이미지 정리에서 삭제하지 않음.
- 정적 `infra/k8s/apps/youtube-memo.yaml`은 초기 준비용 sentinel manifest임. 현재 production Deployment·Service·PVC에 정적 `youtube-memo.yaml`을 재적용하지 않음.
- Docker Compose의 `youtube-memo` 서비스 정의는 검증된 롤백 자산으로 유지함. Caddy는 중지된 Docker rollback 컨테이너의 healthy 상태를 기다리지 않도록 `youtube-memo` Compose `depends_on`을 사용하지 않음.
- K3s에서 새 쓰기가 발생한 이후에는 Docker를 단순 재기동하지 않음.

## 뉴스 수집 K3s 현재 운영 기준

- `news.len.pe.kr`의 확정 경로는 Cloudflare Tunnel → Caddy → K3s `crawler-worker` Service이며, K3s Pod만 production writer로 사용함.
- `/var/lib/personal-server/k3s-runtime-services.state`의 root 소유 runtime state marker에 `crawler-worker=k3s`를 유지함. marker가 없거나 신뢰할 수 없으면 HomeOps와 배포 자동화는 Docker 재기동을 fail-closed로 차단해야 함.
- K3s Service의 정적 ClusterIP는 Git·문서에 기록하지 않으며, 실행 시 Kubernetes API로 조회함.
- `data/crawler-worker`의 `news_archive.json`, `news_collection_status.json`, `news_summaries.sqlite3`은 Docker 롤백·증적 자산으로 보존함. 일상 정리·자동 배포·이미지 정리에서 삭제하지 않음.
- Docker Compose의 `crawler-worker` 서비스 정의와 중지된 Docker 컨테이너는 검증된 롤백 자산으로 유지함. K3s에서 새 수집 상태가 기록된 뒤 Docker `crawler-worker`를 다시 기동하지 않음.

아래 절차는 완료된 이전의 전환 기록임. 현재 K3s production writer를 다시 전환하거나 정적 manifest를 재적용하는 데 사용하지 않음.

1. Docker health와 K3s writer 부재를 확인하고, `news_archive.json`·`news_collection_status.json`을 포함한 데이터 디렉터리 구조를 검증함. 자동 배포·HomeOps 복구·다른 운영자의 수동 재시작이 전환 중 실행되지 않도록 유지보수 구간을 확보하고 진행 중 작업 종료를 확인함. 2. 새 HomeOps 이미지에 N100 override의 runtime state 디렉터리 read-only mount를 적용함. 이 단계는 `homeops-executor`만 `--no-deps`로 갱신하며 Caddy·crawler 전체 배포를 실행하지 않음. `/var/lib/personal-server`와 state 파일은 root 소유·group/other 쓰기 금지를 유지함. 디렉터리 mount로 파일의 원자적 교체도 실행 중 프로세스에 반영됨. 3. Docker crawler 중지 **이전**에 root 소유 runtime state marker를 `crawler-worker=k3s`로 갱신함. Books·YouTube 값은 유지함. 이 설정은 Docker를 중지하지 않으며 자동 재기동 대상에서만 제외함. 실행 중 HomeOps의 `allowed_services()`에서 crawler가 제외됨을 확인함. cutover `--go`는 이 제외 상태를 실제 컨테이너에서 조회할 수 없거나 crawler가 허용되면 Docker를 중지하기 전에 실패함. 4. Docker crawler를 중지해 scheduler와 HTTP writer를 함께 멈춤. 빈 PVC에 전체 디렉터리를 한 번만 복사하고 원본·PVC의 전체 디렉터리 SHA-256 digest를 비교함. SQLite 파일이 실제 존재하면 `quick_check`도 확인함. 5. digest가 일치할 때만 K3s Deployment를 replica 1로 확장함. rollout 이후 Docker 중지와 HomeOps 제외 상태를 다시 검증함. Docker가 재활성화되면 성공을 반환하지 않으며 두 writer를 멈춘 뒤 데이터가 같은 경우에만 Docker 복구를 허용함. 데이터가 달라졌으면 둘 다 중지하고 수동 판단이 필요함. 6. K3s Deployment rollout, Service·Endpoint·PVC readiness와 인증된 native metrics target을 확인함. Caddy upstream을 조회·검증하여 적용하고 Caddy 내부 health를 확인한 뒤 Tunnel의 뉴스 ingress를 Caddy 경로로 단일 전환함. 7. 외부 health 3회를 모두 확인하고 유지보수를 해제함. HomeOps는 매 작업과 Docker restart 직전에 marker를 재조회하며 K3s 소유 서비스를 제외함. marker 누락·오염·권한 이상 시 crawler·Books·YouTube Docker 재시작을 차단함. 전환 실패 후 Docker로 복구됐더라도 marker는 자동으로 compose로 되돌리지 않으며 데이터·writer 상태를 확인한 운영자가 결정함.

현재 K3s에서 새 수집 상태가 기록된 이후 Docker를 단순 재기동하는 rollback은 허용하지 않음. Docker와 PVC 데이터의 단일 source of truth가 다시 확인되지 않은 경우 데이터 분기 위험이 있으므로, 공개 경로만 즉시 되돌리거나 Docker writer를 재기동하지 않음. 향후 rollback은 별도 승인과 데이터 동등성 검증이 필요함.

## 뉴스 수집 관측성

`crawler-worker`는 수집 상태를 `/data/crawler-worker/news_collection_status.json`에 원자적으로 저장함. 상태 파일에는 시각과 실패 횟수만 기록되며 기사·URL·예외 원문·토큰은 포함하지 않음.

Crawler 관측성 manifest `infra/k8s/sre-telegram/crawler-news-observability.yaml`는 native `crawler-worker` Service를 선택함. 실제 ServiceMonitor·PrometheusRule 적용 상태는 이 전환 검증 범위에서 재확인하지 않았으므로, 알림 경로를 변경하기 전에는 아래 읽기 전용 명령으로 확인 필요함. 인증은 Secret의 `bearer_token` 키를 참조하며 값은 문서·Git에 기록하지 않음. `/internal/metrics`는 정확한 Bearer 인증이 없으면 404를 반환함.

시딩 계약은 monitoring namespace Secret `crawler-news-metrics`의 `bearer_token` 키와 crawler runtime의 `NEWS_METRICS_BEARER_TOKEN`에 동일한 승인된 값을 주입하는 것임. 값 자체는 이 문서·Git·로그에 기록하지 않음. 적용 대상은 새 `crawler-news-observability.yaml`의 `ServiceMonitor`와 갱신된 `prometheus-rule.yaml`임.

검증 순서:

1. crawler 상태 파일의 최근 시도·성공·실패 횟수를 확인함. 2. 인증된 내부 metrics 응답에 고정 지표만 노출되는지 확인함. 3. Prometheus에서 `NewsCollectionStale`의 수집·평가 상태를 확인함. 4. Alertmanager → SRE relay의 장애·복구 한국어 메시지를 확인함.

적용 전후 검증 명령은 다음 리소스와 키만 확인하며 Secret 값은 출력하지 않음.

```bash
kubectl -n monitoring get secret crawler-news-metrics -o jsonpath='{.data.bearer_token}' >/dev/null
kubectl -n personal-server get service crawler-worker -l app.kubernetes.io/name=crawler-worker
kubectl -n personal-server get endpointslice -l app.kubernetes.io/name=crawler-worker
kubectl -n monitoring apply --dry-run=client -f infra/k8s/sre-telegram/crawler-news-observability.yaml >/dev/null
kubectl -n monitoring apply --dry-run=client -f infra/k8s/sre-telegram/prometheus-rule.yaml >/dev/null
kubectl -n monitoring get servicemonitor crawler-news-observability
kubectl -n monitoring get prometheusrule sre-telegram-k3s-alerts
kubectl -n monitoring get servicemonitor crawler-news-observability -o yaml | rg 'crawler-worker|internal/metrics|bearerTokenSecret'
```

별도 승인 적용 절차:

```bash
kubectl -n monitoring apply -f infra/k8s/sre-telegram/crawler-news-observability.yaml
kubectl -n monitoring apply -f infra/k8s/sre-telegram/prometheus-rule.yaml
kubectl -n monitoring get servicemonitor crawler-news-observability
kubectl -n monitoring get prometheus -o name
```

적용 전 Secret key와 native `crawler-worker` Service·EndpointSlice, K3s Deployment·PVC readiness를 확인하고 값은 출력하지 않음. K3s writer가 준비되지 않았으면 ServiceMonitor를 적용하지 않음. 적용 후 `ServiceMonitor` 상태와 Prometheus target의 `crawler-worker` 및 `/internal/metrics` 수집 상태를 확인함.

롤백 시 crawler 직전 이미지를 복귀한 뒤 `ServiceMonitor`와 `NewsCollectionStale` 규칙만 제거함. 실제 적용·Secret 생성·배포는 별도 승인 필요함.

```bash
kubectl -n monitoring delete servicemonitor crawler-news-observability
kubectl -n monitoring patch prometheusrule sre-telegram-k3s-alerts --type=json -p='[{"op":"remove","path":"/spec/groups/0/rules/5"}]'
```

## Portal HTTP 관측성

Portal은 `/internal/metrics`에서 요청 수와 요청 지연시간 histogram만 Prometheus 형식으로 제공함. 지표 label은 `method`, 정규화된 route template, `status_code`만 사용함. raw path, query string, IP, 사용자 식별자, 인증정보, 요청·응답 본문, 예외 원문은 지표에 기록하지 않음.

`/internal/metrics`는 `PORTAL_METRICS_BEARER_TOKEN`과 일치하는 Bearer 인증이 없는 경우 404를 반환함. Portal NodePort는 Caddy를 통해 공개 경로로 전달될 수 있으므로, 내부 경로명만으로 비공개 경계가 성립한다고 간주하지 않음.

Prometheus 수집은 `infra/k8s/sre-telegram/portal-http-observability.yaml`의 `ServiceMonitor`를 별도 승인 후 적용함. 이 리소스는 `personal-server` namespace의 `app.kubernetes.io/name=portal-web` Service와 `http` port만 선택함. 인증은 monitoring namespace Secret `portal-http-metrics`의 `bearer_token` 키를 참조하며, 값은 문서·Git·로그에 기록하지 않음.

Kubernetes Secret은 namespace 간 공유되지 않음. 따라서 동일한 승인된 bearer 값은 다음 두 Secret에 운영자 절차로 각각 시딩해야 함.

| Namespace | Secret | Key | 사용처 |
|---|---|---|---|
| `monitoring` | `portal-http-metrics` | `bearer_token` | ServiceMonitor의 Prometheus scrape 인증 |
| `personal-server` | `portal-http-metrics` | `bearer_token` | Portal Pod의 `PORTAL_METRICS_BEARER_TOKEN` 환경변수 |

적용 전에는 두 namespace의 Secret key 존재 여부와 ServiceMonitor dry-run만 수행함. Portal Deployment에는 `personal-server/portal-http-metrics`의 `bearer_token`을 `PORTAL_METRICS_BEARER_TOKEN`으로 참조하는 환경변수만 추가함. 기존 `portal-web-runtime` Secret, Portal cutover, Caddy, Tunnel 변경은 별도 승인 범위임.

```bash
kubectl -n monitoring get secret portal-http-metrics -o jsonpath='{.data.bearer_token}' >/dev/null
kubectl -n personal-server get secret portal-http-metrics -o jsonpath='{.data.bearer_token}' >/dev/null
kubectl -n personal-server get service portal-web -l app.kubernetes.io/name=portal-web
kubectl -n monitoring apply --dry-run=client -f infra/k8s/sre-telegram/portal-http-observability.yaml >/dev/null
```

적용 후에는 일반 Portal 요청을 최소 1회 발생시킨 뒤 Prometheus target의 `portal-web` 수집 상태와 `portal_http_requests_total`, `portal_http_request_duration_seconds` 지표의 존재를 확인함. 초기 요청 전에는 HELP·TYPE 선언만 존재할 수 있음. 값에 인증정보 또는 식별 정보가 포함되면 즉시 적용을 중지하고 외부 노출 없이 원인을 검토함.

### 백업 보관 정책 보완 작업

백업 파일의 누적 용량을 제한하기 위해 최근 일별 7개·이전 4주 구간별 1개·더 오래된 지난 3개월 월별 1개를 보존하는 정책을 구현함. 보관 구간·최신 7개 안전 하한·원격 용량 사전 계산·운영 적용 단계는 [백업 보관 정책 보완](reviews/20261009_백업보관정책_보완.md)을 참조함.
