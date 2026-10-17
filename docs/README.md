# 운영 문서 색인

서비스 구조·현재 기능·운영 절차를 찾기 위한 문서 안내임. 작업 날짜·PR별 진행 나열 대신 각 주제의 현재 기준과 검증 범위를 사용함.

## 현재 운영 기준

| 문서 | 사용할 때 |
|---|---|
| [현재 운영 로드맵](operations-roadmap.md) | 완료 기준과 향후 운영 개선 항목 확인 |
| [SLI·SLO·에러 버짓 운영 기준](slo-baseline.md) | 현재 측정 가능한 운영 신호의 목표값·버짓·월간 검토 기준 확인 |
| [안전한 복구 훈련](recovery-drill.md) | 월간 백업·알림·격리 Pod 복구 점검 |
| [운영 참조](operations-reference.md) | 전체 서비스 구조, 공개 경로, 일상 상태 확인 |
| [차량 DB 백업과 자동 보관](operations-reference.md#차량-db-백업과-자동-보관) | 02:30 다섯 단계 순서, 변경 없음·보관 정리·누락 감지·관리자 상태와 미관찰 경계 |
| [차량 백업 운영 도구](../car-care-worker/tools/automation/README.md) | 잠금·증적·실행 기록 정리·모니터의 상세 운영 계약 |
| [운영 상태 확인 기록](operations-reference.md#운영-상태-확인-기록) | 02:30 순차 백업·Portal 인증 복구의 최신 확인과 SRE·SLO 과거 조회 구분 |
| [컨테이너 실행 권한](operations-reference.md#컨테이너-실행-권한) | non-root 실행 계정과 Portal PVC 사전검증 기준 |
| [N100 운영 환경](n100-mt4-setup.md) | Windows·WSL2 자동 시작, 제한형 자동복구, 재부팅 뒤 확인, 자원 점검 |
| [K3s·모니터링·백업](../infra/k8s/README.md) | Portal K3s, Grafana, Telegram SRE relay, PVC 백업 |
| [Book Memo K3s 현재 운영 기준](operations-reference.md#book-memo-k3s-현재-운영-기준) | 확정 공개 경로, runtime state marker, 롤백 자산 보존, 정적 manifest 재적용 금지 확인 |
| [YouTube Memo K3s 현재 운영 기준](operations-reference.md#youtube-memo-k3s-현재-운영-기준) | 확정 공개 경로, runtime state marker, 롤백 자산 보존, 정적 manifest 재적용 금지 확인 |
| [뉴스 수집 K3s 현재 운영 기준](operations-reference.md#뉴스-수집-k3s-현재-운영-기준) | 확정 공개 경로, K3s 단일 writer, runtime marker, 롤백 자산 보존 기준 확인 |
| [뉴스 수집 관측성](operations-reference.md#뉴스-수집-관측성) | crawler 수집 상태·인증 metrics·NewsCollectionStale 운영 기준 |
| [Cloudflare Tunnel](cloudflare-tunnel.md) | Caddy 경유 Portal·Crawler Worker·Book Memo·YouTube Memo, 차량 callback 경계와 터널 장애 대응 |
| [공개 상태 Telegram 알림](public-uptime-monitor.md) | 약 5분 외부 점검과 N100 직접 Tunnel 전환 알림 조건 |
| [N100 안전 자동 배포](n100-github-auto-deploy.md) | 허용 서비스의 GitHub Actions 배포 |
| [N100 원격 개발](n100-remote-development.md) | Mac에서 N100 WSL 작업 환경 사용 |
| [작업 인수인계](agent-handoff.md) | 저장소 구조와 작업 경계 확인 |
| [Codex 작업 완료 루프](codex-work-loop.md) | 변경·검증·PR 절차 |
| [작업 루프 증거 운영](agent-loop-evidence.md) | CI 결과와 증적 보관 기준 |

## 포트폴리오와 현재 개발 상태

| 문서 | 내용 |
|---|---|
| [DevOps 포트폴리오](portfolio-devops.md) | 기술 선택·문제 해결·면접 답변과 검증 한계 |
| [현재 개발 계획](20260921_프로젝트보완_개발계획.md) | 완료 기능·남은 검증·운영 승인 경계 |
| [통합 검색 로컬 성능 실습](search-performance-lab.md) | 실제 검색 함수의 부하·부분 실패·지연 측정과 운영 용량 해석의 한계 |
| [로컬 장애 진단·복구 실습](incident-recovery-lab.md) | 실제 검색 지연·SQLite 잠금·페이지 제한, 복구·보존·정리와 시간선 |
| [문서 이미지 목록](images/README.md) | 구성도·스크린샷의 용도와 표시 범위 |

## 문서 갱신 원칙

- 현재 기능과 운영 방식은 코드·설정·실제 검증 근거를 대조해 작성함.
- 적용 완료와 추가 확인이 필요한 항목을 구분함.
- 날짜별 작업일지와 PR·승인 과정을 안내 본문에 누적하지 않음.
- 운영 변경은 저장소 승인 기준에 따르며 문서 수정으로 새 승인을 부여하지 않음.
- 비밀번호·토큰·Secret 값·개인 접속 정보는 기록하지 않음.
