# Task 1 완료 보고

## 변경 범위

| 구분 | 내용 |
|---|---|
| 구현 | `scripts/run-n100-operations.sh` 추가 |
| 테스트 | `tests/test_n100_operations.py` 추가 |
| 허용 작업 | `diagnose`, `deploy_safe_crawler`, `verify_news_observability`, `apply_news_observability` |
| 금지 경계 | 임의 명령, Secret 값 출력, 서버·scheduler·Caddy·Tunnel 설정 변경 미지원 |

## 검증 결과

- `bash -n scripts/run-n100-operations.sh` 통과함
- `python3 -m unittest tests.test_n100_operations -v` 통과함 (6건)
- 알 수 없는 operation 및 추가 인자 거부 검증함
- 적용 대상이 고정된 두 manifest인지, `sudo -n k3s kubectl` 경계인지, Secret 값이 출력되지 않는지 mock 검증함

## 커밋

- `120b70c feat: N100 제한형 운영 실행기 추가`

## 잔여 위험 및 확인 필요 사항

- 실제 N100에서 `diagnose`, Secret key 사전 시딩 상태, crawler runtime token, K3s 권한을 실행 검증하지 않음.
- `deploy_safe_crawler` 실행은 기존 안전 배포 스크립트의 운영 환경과 GitHub Actions checkout 상태에 의존함.
- workflow와 운영 문서는 Task 2/3에서 추가 예정임.
