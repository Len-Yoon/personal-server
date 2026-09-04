# N100 전환 실행기 설치·프리플라이트

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | N100 전환 실행기 설치·프리플라이트 |
| 작성일 | 2026-09-04 |
| 기준 자료 | `infra/k8s/transition-runner/` release artifact |
| 목적 | 검토된 root 실행기와 암호화 credential의 1회 설치 절차 정의 |
| 비고 | 운영 승인 전에는 읽기 전용 preflight만 수행 |

## 핵심 요약

설치기는 명시적 `--apply`와 root 권한을 요구하며, native ext4의 고정 경로에만 root 소유 release를 원자적으로 설치함. 실행기 release digest와 credential 파일 권한·소유자를 설치 전에 확인함.

## 프리플라이트

프리플라이트는 기본적으로 읽기 전용이며 서비스명, 검사명, PASS/FAIL 상태만 출력함. credential 값, 파일 내용, 환경변수 값은 읽거나 출력하지 않음.

```bash
bash infra/k8s/tools/transition-runner-preflight.sh
```

## root 설치

운영자는 runner·policy·validator·systemd unit 전체 release manifest의 SHA-256 값을 확인하고, root 소유 `0600` 암호화 credential 파일이 있는 native ext4 N100 경로를 준비해야 함. 다음 명령은 예시이며 digest와 credential 경로는 실제 승인 값으로 대체 필요함.

```bash
sudo bash infra/k8s/tools/install-transition-runner.sh --apply \
  --release-digest sha256:<64 lowercase hex> \
  --credential-dir /secure/personal-server-transition/credentials
```

설치기는 저장소의 runner를 root runtime으로 실행하지 않으며, 설치 후에는 복사된 release artifact만 참조함. 실제 root 설치, credential seed, systemd 활성화·실행, data/PVC 및 Caddy/public route cutover는 별도 N100 운영 승인 범위임.

## 확인 필요 사항

- 실제 root 설치는 N100 운영자 승인과 유지보수 창이 필요함.
- credential seed 및 암호화 방식의 운영 절차는 별도 승인 필요함.
