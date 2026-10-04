# 운영 문서 색인

이 색인은 현재 운영 문서의 진입점임. 개발 계획과 시점별 검증 기록은 별도 항목에서 관리함. 서비스명·도메인·운영 절차는 아래 문서를 기준으로 하며, 비밀값은 어떤 문서에도 기록하지 않음.

## 현재 운영 기준

| 문서 | 사용할 때 |
|---|---|
| [현재 운영 로드맵](operations-roadmap.md) | 완료 기준과 향후 운영 개선 항목 확인 |
| [SLI·SLO·에러 버짓 운영 기준](slo-baseline.md) | 현재 측정 가능한 운영 신호의 목표값·버짓·월간 검토 기준 확인 |
| [안전한 복구 훈련](recovery-drill.md) | 월간 백업·알림·격리 Pod 복구 점검 |
| [운영 참조](operations-reference.md) | 전체 서비스 구조, 공개 경로, 일상 상태 확인 |
| [운영 상태 확인 기록](operations-reference.md#운영-상태-확인-기록) | 백업·SRE·SLO 실행 주체와 마지막 확인 결과 |
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

## 최신 반영·검증 기록

[DevOps 포트폴리오](portfolio-devops.md)는 면접용 설계 판단·검증 근거·예상 질문을 정리함. Loki·Ansible 후속 실습은 [PR #331](https://github.com/Len-Yoon/personal-server/pull/331)의 CI·Trivy·독립 검토 통과 후 `f66a42a`로 병합됨. 승인된 N100 적용과 Loki 샘플 조회·Ansible 멱등성·롤백 리허설을 완료함. 최초 조회 실패와 미검증 항목은 [운영 검증 결과](reviews/20261004_Loki_Ansible_실습_N100운영검증결과.md)에 기록함.

| 문서 | 확인 내용 |
|---|---|
| [서비스별 PVC 백업 운영 목표 상태 동기화 결과](reviews/20260928_백업운영목표상태_동기화결과.md) | 맥 기준 목표 상태의 N100 동기화·검증, 쓰기 0건, 독립 운영 검토와 외부 health |
| [서비스별 PVC 백업 자동 실행 적용 결과](reviews/20260928_서비스별_PVC_백업_자동실행_적용결과.md) | N100 세 CronJob 활성화·즉시 Job 미생성·외부 health와 첫 정기 실행 확인 필요 사항 |
| [서비스별 PVC 백업 자동화 사전 검토](reviews/20260928_서비스별_PVC_백업_자동화_사전검토.md) | Drive 공유 범위·중복 폴더 해소·Mac 복호화와 자동 실행 전 조건 |
| [서비스별 PVC 원격 백업 N100 수동 검증 결과](reviews/20260928_서비스별_PVC_백업_N100_수동검증결과.md) | Book·YouTube·Crawler의 암호화 백업·격리 복원 성공과 자동 실행 보류 상태 |
| [2026-09-27 고도화·N100 적용 결과](20260921_프로젝트보완_개발계획.md#2026-09-27-a2a4a5-고도화-및-n100-적용-결과) | A2 뉴스 archive 보호, A3 검색 병렬화, A4 Book·YouTube 페이지네이션, A5 Portal 파일 경계의 저장소·운영 상태와 백업 후속 검증 |
| [통합 검색 병렬화 검증 결과](reviews/20260927_통합검색병렬화_검증결과.md) | A3의 로컬 회귀·지연 측정 조건과 운영 성능 측정 제외 범위 |
| [뉴스·SLO 운영 적용 결과](reviews/20260922_뉴스_SLO_운영적용_검증결과.md) | 10차 뉴스 호환·목표 이미지 적용과 일일 SLO 자동 실행 활성화 |
| [Book·Portal K3s 배포 결과](reviews/20260921_K3s앱배포_검증결과.md) | 당시 이미지 적용·데이터 보존·외부 health와 뉴스 보류 이력 |
| [차량 배포 결과](reviews/20260921_차량배포_검증결과.md) | health 설정·멱등성 변경의 실제 운영 적용 |
| [뉴스 복구 호환 검토안](reviews/20260921_뉴스복구호환_적용검토안.md) | 2026-09-22 적용 전 rollback 호환성 검토 이력 |
| [SLO 증적 실패 원인](reviews/20260921_SLO증적실패_원인분석.md) | 적용 전 다중 시계열 원인·수정안 검토 이력 |
| [문서 이미지 목록](images/README.md) | 전체 그림·스크린샷의 용도와 최신성 |
| [문서·이미지 정합성 점검](reviews/20260921_문서이미지_정합성점검.md) | 문서 반영 누락·그림 검토·검증 범위 |

## 문서 갱신 원칙

- README와 운영 문서는 코드·설정·실행 검증으로 확인된 현재 사실만 기록함.
- 이전 설계안, 완료된 구현 계획, 폐기된 전환 초안은 운영 문서에 보관하지 않음.
- Portal, K3s, Caddy, Secret, PVC, 운영 데이터 변경 절차는 자동 배포 문서와 분리함.
- 비밀번호, 토큰, chat ID, Secret 값, 개인 경로는 기록하지 않음.

## 개발 계획과 후속 운영 작업

- [2026-09-21 프로젝트 보완 개발 계획](20260921_프로젝트보완_개발계획.md): 종합 점검 F01~F15의 순차 실행, 승인 경계, 단계별 검증·진행 상태를 관리함.
