# P3-A GitHub Action SHA 고정 보고서

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | P3-A GitHub Action SHA 고정 보고서 |
| 작성일 | 2026-09-10 |
| 기준 자료 | 사용자 지정 Action SHA 목록 및 `.github/workflows/*.yml` |
| 목적 | 외부 GitHub Action 참조의 immutable full commit SHA 고정 |

## 2. 핵심 요약

- `.github/workflows/*.yml`의 외부 Action 참조를 지정된 40자리 commit SHA로 고정함.
- 각 참조에 원래 메이저 버전 주석을 추가함.
- Dockerfile, Caddy, N100 실행 절차, scheduler, secrets, 배포 조건은 변경하지 않음.

## 3. 변경 상세

| Action | 고정 SHA | 버전 주석 |
|---|---|---|
| `actions/checkout` | `11d5960a326750d5838078e36cf38b85af677262` | `v4` |
| `actions/setup-python` | `a26af69be951a213d495a4c3e4e4022e16d87065` | `v5` |
| `actions/upload-artifact` | `ea165f8d65b6e75b540449e92b4886f43607fa02` | `v4` |
| `actions/download-artifact` | `d3f86a106a0bac45b974a628896c90dbdf5c8093` | `v4` |
| `actions/github-script` | `f28e40c7f34bde8b3046d885e986cb6290c5673b` | `v7` |

## 4. 검토 결과

| 검토 항목 | 결과 | 비고 |
|---|---|---|
| 외부 Action SHA·버전 주석 검증 | 일치 | CI 유지보수 테스트에 검증 추가함 |
| 관련 CI 테스트 | 일치 | CI 매트릭스 406건 통과함 |
| YAML 정적 검사 | 일치 | 모든 `.github/workflows/*.yml` 파싱 성공함 |
| 변경 범위 검증 | 검토 준비 | `run_change_harness.py` 결과 `ready_for_review` 확인함 |
| 범위 제외 항목 | 일치 | Dockerfile, Caddy, N100 실행 절차, scheduler, secrets, 배포 조건 변경 없음 |

## 5. 확인 필요 사항

- 없음.

## 6. 후속 조치

- 변경 사항을 로컬 커밋으로 기록함. Push 및 merge는 수행하지 않음.
- `car-care-worker` 콜백 테스트는 격리 환경의 loopback 포트 바인딩 제한으로 최초 4건 오류 발생했으며, 일반 실행 권한 재검증에서 53건 모두 통과함.
