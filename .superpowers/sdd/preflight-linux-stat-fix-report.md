# Linux `stat` 모드 판독 수정 보고서

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | Linux `stat` 모드 판독 수정 보고서 |
| 작성일 | 2026-09-02 |
| 작성자 | Codex |
| 기준 자료 | `infra/k8s/tools/sre-telegram-preflight.sh`, `tests/test_k8s_sre_telegram_tools.py` |
| 목적 | GNU/Linux에서 0600 운영자 설정 파일을 정상 판독하도록 수정 |
| 비고 | N100 설정 내용은 읽거나 출력하지 않음 |

## 핵심 요약

macOS용 `stat -f '%Lp'`를 먼저 실행하던 로직이 GNU/Linux에서 0으로 종료하면서 모드가 아닌 문자열을 반환해 0600 파일을 거부하던 문제를 수정함. `uname -s` 결과에 따라 macOS는 `stat -f`, Linux는 `stat -c '%a'`를 사용하도록 변경함.

## 변경 내용

| 구분 | 대상 | 변경 내용 | 검증 결과 |
|---|---|---|---|
| 테스트 | `tests/test_k8s_sre_telegram_tools.py` | GNU `stat`의 `-f` 오동작과 `-c '%a'`의 600 반환을 재현하는 실행형 회귀 테스트 추가 | RED 재현 후 GREEN 통과 |
| 구현 | `infra/k8s/tools/sre-telegram-preflight.sh` | 플랫폼별 `stat` 형식 선택, 미지원 플랫폼·`uname` 실패는 실패 처리 | 통과 |

## 검토 결과

- 기존 회귀 테스트는 변경하지 않음.
- 모드가 정확히 `600`이 아니거나 모드 판독이 불가능한 경우 기존 fail-closed 동작 유지됨.
- 서버 시작·스케줄러 영역은 변경하지 않음.

## 검증 결과

| 검사 | 결과 |
|---|---|
| 회귀 테스트(RED) | 통과된 실패 재현: 기존 구현에서 return code 1 확인 |
| 회귀 테스트(GREEN) | 통과 |
| `python3 -m unittest tests.test_k8s_sre_telegram_tools` | 31건 통과 |
| `bash -n infra/k8s/tools/sre-telegram-preflight.sh` | 통과 |
| `git diff --check` | 통과 |
| 변경 하네스 | `ready_for_review` |

## 확인 필요 사항

없음.

## 후속 조치

커밋 후 상위 작업에서 독립 검토 및 통합 검증 예정.
