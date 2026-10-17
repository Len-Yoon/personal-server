# 통합 검색 로컬 성능 실습

## 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 작성일 | 2026-10-17 | Asia/Seoul |
| 목적 | 실제 검색 집계 함수의 부하·부분 실패·지연을 고정 조건에서 측정함 | 운영 부하 시험 아님 |
| 기준 자료 | [설계서](superpowers/specs/20261017_통합검색성능실습_설계서.md), [구현 계획](superpowers/plans/20261017_통합검색성능실습_구현계획.md) | 클라우드·새 VM 제외 |

## 핵심 요약

실제 `portal-web/app/services/global_search.py`의 `search_all`을 로컬 샘플 HTTP 서버 세 개와 연결함. 제품 검색 로직·timeout·부분 실패 표현을 그대로 사용함. 샘플 서버와 부하 발생기는 셀마다 별도 프로세스에서 실행하며 운영 앱·데이터·자격 증명에 접근하지 않음.

## 상세 내용

### 실행

저장소 루트에서 기존 Portal 테스트 환경을 사용함. 서버 socket의 loopback 바인딩이 허용된 환경이 필요함. sandbox가 막으면 결과는 실패·미검증으로 남기며 요청을 가짜 결과로 대체하지 않음.

```bash
# 작은 실제 HTTP 연결 검사: normal, 동시성 1, 준비 5건, 측정 5건
.venv/portal/bin/python portal-web/tools/search_performance_lab.py \
  --smoke --output /tmp/search-smoke.json

# 기본 행렬: 네 시나리오 × 동시성 1·5·10 × 반복 3회
.venv/portal/bin/python portal-web/tools/search_performance_lab.py \
  --output /tmp/search-baseline.json

# 특정 조건만 재측정: 선택한 각 동시성에 동일한 준비·측정 적용
.venv/portal/bin/python portal-web/tools/search_performance_lab.py \
  --scenario timeout --concurrency 1 --concurrency 5 \
  --count 10 --repeats 1 --deadline 120 --output /tmp/search-timeout.json
```

결과 파일은 신규 파일만 허용함. 기존 파일·symlink를 덮어쓰지 않음. 경로가 없거나 파일이 이미 있으면 socket을 시작하지 않음. `--smoke`도 입력 상한 검증을 우회하지 않음. 환경을 새로 설치하는 작업은 이 도구에 포함되지 않음.

### 시나리오·상한

| 구분 | 설정·동작 | 비고 |
|---|---|---|
| normal | 세 대상 모두 고정 JSON 반환 | 동시성 1에서는 정확한 전체 결과 필요 |
| delayed | books만 500ms 지연 후 정상 응답 | 부하가 크면 예산 초과가 관측될 수 있음 |
| timeout | books만 2초 지연 | books unavailable, 다른 두 대상 정상 기대 |
| http_failure | books가 HTTP 503 반환 | books unavailable, 다른 두 대상 정상 기대 |
| 동시성 | 기본 1·5·10, 최대 세 종류·각 1~10 | 중복 거부 |
| 측정 건수 | 셀당 기본·최대 30 | 준비 호출 5건 별도 |
| 반복 | 기본·최대 3 | 서로 다른 셀 통계를 섞지 않음 |
| 실행 제한 | 최대 600초 | 완료 보장 시간이 아님. 초과 시 미실행 셀과 부분 결과 기록 |
| 정리 제한 | 종료 유예 5초, 종료·강제 종료·회수에 추가 최대 5초 | 소유한 자식만 종료함 |

모든 검색 URL을 부모가 확보한 loopback socket으로 고정하고 demo·proxy 환경을 자식에게 상속하지 않음. redirect나 외부 대상 URL 입력 옵션이 없음. 샘플 반환 URL은 `#`이며 검색어·응답 원문·환경 전체·계정·개인 경로는 증적에 넣지 않음.

### 증적 해석

| 필드 | 의미 | 비고 |
|---|---|---|
| `valid`·`reason` | 실험 수집·고정 계약·정리의 성공 여부 | 운영 서비스 정상·성능 SLO 합격과 다름 |
| `environment` | Git SHA·dirty·실행기 해시·Python/httpx·OS·샘플 해시 | dirty이면 SHA만으로 작업 트리를 재현할 수 없음 |
| `configuration` | 선택한 행렬·표본·제한값 | baseline 비교 시 동일하게 유지 |
| `samples` | 개별 측정 지연·대상 순서별 상태·고정 결과 일치 여부 | 대상 순서는 news·youtube·books |
| `summary` | 측정 구간 통계 | 준비 호출 제외 |
| `warmup` | 준비 호출 통계 | 실제 고정 계약도 확인함 |
| `planned/started/completed/cancelled/not_started` | 계획·시작·완료·취소·미시작 건수 | 완료 분모에 미시작·취소를 넣지 않음 |
| `all_ok_ratio` | 정확한 결과와 세 대상 ok를 모두 만족한 완료 호출 비율 | 상태가 ok여도 본문이 틀리면 정상 아님 |
| `partial_failure_ratio` | unavailable 대상이 하나 이상인 완료 호출 비율 | fault 시나리오에서는 의도한 관측값 |
| `contract_violations` | 고정 결과 불일치·예외·정상 대상 유실 건수 | fault의 예상된 부분 실패와 구분 |
| `resources` | 프로세스별 누적 CPU·high-water RSS | 셀 수명·기동·준비 호출 포함. 순간 CPU/RSS·호스트 전체 지표 아님 |
| `resource_missing` | 수집하지 못한 자원 지표 | 0이나 성공으로 채우지 않음 |
| `cleanup_ok` | 소유 자식 회수 완료 여부 | 운영 자원 정리 결과 아님 |

처리량은 첫 측정 시작부터 마지막 완료까지의 wall time을 사용함. 중단 셀은 중단 확정 시각까지만 분모에 포함하며 정리 시간을 제외함. cutoff 이전에 발행된 pipe 이벤트를 회수하고 이후 완료는 표본에 넣지 않음. 실행 실패 시에도 가능한 부분 증적과 실패 원인을 보존함. 종료 신호는 SIGINT/SIGTERM을 처리하며 SIGKILL·전원 상실에서 파일 완결성을 보장하지 않음.

### 재집계 예시

```bash
.venv/portal/bin/python - <<'PY'
import json
import math
from pathlib import Path
report = json.loads(Path('/tmp/search-baseline.json').read_text())
for cell in report['cells']:
    values = sorted(row['latency_seconds'] for row in cell['samples'])
    p95 = values[math.ceil(len(values) * .95) - 1] if values else None
    print(cell['scenario'], cell['concurrency'], cell['repeat'], p95)
PY
```

## 검토 결과와 한계

closed-loop에서는 응답이 느려지면 주입 요청률도 줄어듦. 이 결과로 고정 도착률에서의 최대 처리량·큐 적체·운영 용량을 확정하지 않음. 30건의 p99는 표본 한계를 표시하고 tail latency의 안정적 추정으로 사용하지 않음. 세 반복의 percentile을 평균하지 않으며 각 셀의 수치와 변동을 함께 검토함.

이 실습에는 실제 DB 검색·HTML 렌더링·브라우저·인증·공개 네트워크·N100 자원 경합이 포함되지 않음. macOS와 Linux의 RSS 단위는 정규화하지만 서로 다른 기계의 수치를 직접 개선 전후로 비교하지 않음. 병목 근거가 없으면 제품 클라이언트 공유나 저장 구조 변경을 수행하지 않음.

## 확인 필요 사항

실제 측정 결과와 독립 검토·CI는 결과 보고서로 별도 기록함. 프로세스 누적 자원만으로 순간 병목을 확정할 수 없으며 추가 프로파일링이 필요할 수 있음. 운영 적용은 현재 범위에 포함되지 않음.

## 후속 조치

baseline의 변동·오류·부분 실패·자원 사용을 비교한 뒤 추가 프로파일링 또는 개선 후보 하나를 판단함. 장애 진단·복구와 배포 표준화 단계는 이 도구에서 확인한 증거 계약을 재사용하되 별도 설계함.
