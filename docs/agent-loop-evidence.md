# Codex 작업 완료 루프 증거 운영

변경 계획, 검증, PR 병합 뒤 브랜치 정리 절차는 [Codex 작업 완료 루프](codex-work-loop.md)를 따름.

## 1. 목적

이 문서는 개인서버 저장소에서 Codex 작업 완료 루프를 적용한 결과를 동일한 기준으로 확인·기록하기 위한 운영 기준임. 변경 범위, 서비스별 CI 검증, PR 정책 검토 결과를 작업 증거로 남기며, 자동 판단이 어려운 변경은 사람 검토 대상으로 분류함.

## 2. 로컬 하네스 증적 해석

### 독립 검토의 작성 이력과 대상 고정

최종 독립 검토에는 아래 엄격 모드를 사용함. 기존 경로 사전 점검과 CI 호출은 호환 유지되며 `independent_review.status=not_requested`는 독립 검토 통과를 뜻하지 않음.

```sh
git diff --name-status -z --find-renames <기준커밋> HEAD > /tmp/review-paths.z
python3 scripts/run_change_harness.py --input /tmp/review-paths.z \
  --input-format git-name-status-z --check-result maintenance=success \
  --require-independent-review --review-base <기준커밋> \
  --review-session /tmp/implementer.jsonl --review-session /tmp/reviewer.jsonl \
  --review-receipt /tmp/review-receipt.json
```

실제 필요한 검사마다 `--check-result`를 전달함. 구현·테스트·설정·절차를 작성한 모든 세션과 검토 세션의 Codex JSONL 기록을 제공함. 최초 호출에서는 receipt 옵션을 생략하여 `independent_review.target`을 얻고, 구현에 참여하지 않은 검토자가 실제 diff·원본·검증 결과를 확인한 뒤 그 target 전체와 자신의 세션 UUID를 아래 형식으로 기록함. receipt는 저장소 밖에 보관함.

```json
{"schema_version":1,"target":{"base_commit":"...","head_commit":"...","scope":[],"diff_sha256":"...","diff_format":"git-raw-blob-sha256-v1","activity_sha256":"..."},"reviewer_session_id":"실제 세션 UUID","decision":"pass"}
```

| 검사 | 처리 | 비고 |
|---|---|---|
| 실제 작성 이력 | `session_meta` UUID와 완료된 `FileChange`를 대조함 | 역할명 변경으로 작성 이력이 사라지지 않음 |
| 실행 기록 | 알 수 없는 명령·도구와 미완료 실행은 잠재 작성자로 처리함 | 검토자의 테스트 실행도 보조 검증으로 별도 기록함 |
| 변경 이력 | 기준 이후 모든 커밋의 경로를 확인함 | 중간 작성 후 되돌림·이름 변경도 포함함 |
| 대상 고정 | 기준/대상 커밋·경로·파일 모드·blob SHA256·작성 증거 해시를 대조함 | 새 커밋·관련 쓰기·범위 변경 후 기존 PASS 무효화함 |
| 미확인 상태 | 누락·자기 검토·불일치·검토 실패는 종료 코드 2, 해석 불가 입력은 1 | dirty 작업 트리와 submodule 변경은 지원하지 않음 |

원본 로그의 내용을 실행하거나 명령·diff·비밀값을 출력하지 않음. 실행 중인 로그를 복사할 때는 도구 실행 종료 뒤의 고정된 스냅샷을 사용하고, 이후 작성이 있으면 모든 해당 로그를 갱신하여 재검토함. 읽기 메시지만 추가된 것은 관련 작성 증거를 바꾸지 않음.

이는 운영자가 제공한 로컬 기록 사이의 일관성 검사임. 로그 누락·위조 방지, 실제 사용자 인증, 런타임의 완전한 기록을 보장하지 않으며 receipt도 서명된 승인 토큰이 아님. 읽기 명령 분류는 당시 셸·Git 설정이 신뢰된다는 전제이며, 미지원 도구는 보수적으로 차단함. 작성 이력이 경로에 귀속되지 않으면 확인 필요로 남김. 성공은 PR CI·독립 검토자의 판단·운영 승인을 대체하지 않음. CI에 로컬 세션을 업로드하지 않으며 기존 workflow는 변경하지 않음.

`scripts/run_change_harness.py`는 변경 경로와 호출자가 전달한 검증 결과를 정책 증적으로 정규화함. 하네스 자체는 서버 기동, 스케줄러 실행, 배포·재시작 등 운영 명령을 실행하지 않으며, 검증 명령의 성공 여부를 대신 주장하지 않음. 따라서 하네스 출력은 로컬 작업 판단을 위한 증적이며 운영 실행 결과 또는 CI 결과를 대체하지 않음.

### 2.1 에이전트 작업 컨텍스트 적용 경계

코드·설정 변경 전 변경 경로 파일을 준비한 뒤 아래 명령을 명시적으로 실행하여 작업 상태와 다음 조치를 확인함.

```text
python3 scripts/run_change_harness.py --input <변경경로파일> --agent-context
```

테스트와 적용 가능한 정적 검사 후에는 실제 실행 결과만 `--check-result <검사명>=success` 또는 `--check-result <검사명>=failure`로 전달하여 같은 명령을 다시 실행함. `--agent-context`는 변경 파일 전체 목록과 중첩 정책 JSON을 제외한 간결한 Markdown 요약이므로, 필요 시 작업 컨텍스트에 직접 반영할 수 있음. Codex가 이 출력을 자동으로 작업 컨텍스트에 주입하지 않으므로 실행·반영 책임은 작업자에게 있음.

```text
python3 scripts/run_change_harness.py --input <변경경로파일> --check-result <검사명>=success --agent-context
```

### 2.2 작업 상태별 처리

| `work_status` | 의미 | 처리 |
|---|---|---|
| `input_error` | 입력 형식, 검증 이름 또는 정책 증적을 읽거나 검증할 수 없음 | 성공으로 처리하지 않고 입력을 수정한 뒤 재실행함. `policy_evidence`는 `null`일 수 있음 |
| `blocked` | 금지 경로 또는 미분류 경로가 포함됨 | 작업을 중단하고 담당자 검토를 요청함. 누락·실패 검증이 함께 있으면 차단 사유를 우선하되 모든 사유를 보존함 |
| `verification_incomplete` | 필요한 검증 결과가 전달되지 않음 | 검증을 실행·확인한 뒤 결과를 추가하여 재실행함 |
| `verification_failed` | 필요한 검증 결과 중 실패가 있음 | 실패 원인과 재시도 내역을 기록하고 원인 해소 후 재검증함 |
| `ready_for_review` | 경로가 정책상 허용되고 필요한 검증 결과가 모두 성공함 | 하네스가 최종 승인하지 않으며, 사람 검토·PR 정책·배포 절차를 계속 수행함 |

상태 우선순위는 `input_error`(입력 처리 실패) → `blocked`(금지·미분류) → `verification_incomplete`(누락) → `verification_failed`(실패) → `ready_for_review`(검토 가능)로 해석함. `ready_for_review`만 종료 코드 0이며, 나머지는 사람 확인이 필요한 상태임.

### 2.3 UTF-8 바이트 측정 기준

토큰 원본을 아직 측정하지 않았으므로 실제 모델 토큰 절감률은 확인 필요함. 공통 비교 단위는 다음 UTF-8 바이트 수로 기록함.

| 항목 | 측정 대상 |
|---|---|
| 원본 입력 크기 | 하네스 없이 에이전트에 제공한 변경 목록·정책/테스트 탐색 결과·작업 설명을 UTF-8로 인코딩한 바이트 수 |
| 하네스 입력 크기 | 하네스의 `summary`, `human_review_reasons`, `work_status`를 JSON 직렬화한 UTF-8 바이트 수 |
| 절감률 | `(원본 입력 크기 - 하네스 입력 크기) / 원본 입력 크기 × 100` |
| 수용 목표 | 절감률 25% 이상 |

원본 입력 크기와 하네스 입력 크기의 실제 값, 측정 시점·직렬화 옵션, 대상 모델 토큰 사용량은 별도 측정 후 확인 필요함. UTF-8 바이트 수는 모델 토큰 수와 동일하지 않음.

### 2.4 실제 모델 토큰 측정 기준

UTF-8 바이트 측정은 입력 증적의 크기 비교용이며 모델 사용량·비용을 의미하지 않음. 실제 모델 토큰 절감은 같은 작업·모델·프롬프트 조건에서 수집한 JSONL 기록만으로 집계함.

측정 기록은 `scripts/record_token_measurement.py`가 표준 입력의 단일 JSON 객체를 검증한 뒤 지정한 로컬 JSONL 파일에 추가함. 기존 집계기와 동일한 작업·모델·측정 그룹·프롬프트 지문 및 UTC 시간대 인식 ISO 8601 조건을 적용함. 허용된 측정 필드만 기록하고 입력 단계에서 명백한 비밀값 형태를 거부하며, 검증 실패 시 출력 파일을 변경하지 않음.

```text
printf '%s\n' '<측정 JSON 객체>' | python3 scripts/record_token_measurement.py --output <측정기록.jsonl>
```

```text
python3 scripts/summarize_token_measurements.py --input <측정기록.jsonl>
```

기록 한 줄에는 작업 식별자, 측정 그룹, 모델명, 프롬프트 지문, UTC 시간대 인식 ISO 8601 시각, 원본/하네스의 입력·출력 토큰 수를 포함함. 측정 도구는 누락값, 음수, UTC 미포함 시각 또는 서로 다른 작업·모델·측정 그룹·프롬프트 지문이 섞인 기록을 입력 오류로 처리함. 집계 결과의 `reduction_percent`만 실제 토큰 절감률로 기록하며, 실측 기록이 없으면 `확인 필요`로 분류함.

### 2.5 CI artifact 연계 경계

CI는 변경 목록과 서비스 테스트 결과로 정책 증적을 생성·보관함. CI summary job은 같은 변경 목록으로 하네스를 실행해 구조화 JSON과 `--agent-context` 요약을 artifact에 함께 보관함. 다만 하네스의 `ready_for_review`는 사람 검토 가능 상태이며 CI 성공, PR 승인 또는 배포 승인을 뜻하지 않음. CI가 모델 토큰 사용량을 자동 수집하지 않으므로 실제 토큰 절감률은 로컬 측정 기록을 별도 집계해야 함.

### 2.6 테스트 건너뜀·분기·변이 검증

선택된 CI 테스트 묶음은 일반 실행기 `tests/run_ci_tests.py`와 K8s 실행기 `tests/run_ci_shards.py`에 `--fail-on-skip`을 전달함. 메서드·subtest·모듈/클래스 fixture의 skip, expected failure, unexpected success, 빈 수집과 실행 ID 누락·중복은 성공으로 처리하지 않음. 로컬 기본 실행의 선택적 skip과 문서 전용 변경의 검사 묶음 선택은 유지함. 플랫폼·의존성이 맞지 않아 발생한 CI skip은 환경을 보완하며 허용 목록으로 숨기지 않음.

| 증거 | 확인 내용 | 한계 |
|---|---|---|
| `test-results-<묶음>` | 수집/실행 ID, skip·실패·오류의 이유, strict 모드와 성공 여부 | 등록된 테스트의 실행 증거이며 코드 경로 전체의 검증률이 아님 |
| `k8s-shard-evidence` | 같은 결과를 각 worker와 집계에서 대조함 | worker 누락·잘못된 보고서는 실패함 |
| `verification-quality-evidence` | `coverage.json`, `quality.json`, `quality.txt`의 원본·문장/분기·변이 결과 | 아래에 고정한 일부 위험 경계의 검사임 |

`quality` CI job은 문서 전용 변경을 제외한 변경에서 필수 실행하며, summary가 결과 성공을 요구함. 실패 시에도 artifact 업로드를 시도함. 분기 측정은 [coverage.py의 branch 방식](https://coverage.readthedocs.io/en/7.16.2/branch.html)을 사용하고, 도구 버전·기존 테스트 ID·측정 대상·하한은 [고정 설정](../tests/verification_quality.json)과 [고정 의존성](../tests/requirements.quality.txt)에서 대조함.

```sh
python3 tests/run_ci_tests.py --suite maintenance --evidence /tmp/maintenance-results.json --fail-on-skip
python3 tests/run_ci_shards.py --workers 4 --evidence /tmp/k8s-results.json --fail-on-skip
python3 scripts/run_verification_quality.py --output-dir /tmp/verification-quality
```

품질 도구는 인증·업로드 저장소·백업 증적 검증의 실제 소스를 명시한 코드 allowlist로만 임시 복사함. 운영 환경 변수·HOME·데이터·자격 증명을 복사하지 않고, 테스트 환경의 저장·로그 경로를 임시 디렉터리로 지정하며 네트워크 연결을 차단함. 원본 해시를 전후 대조하고 종료 시 임시 복사를 정리함.

분기 증거에는 세 파일 전체와 핵심 함수의 문장/분기 실측값, 실행되지 않은 행·분기 arc를 함께 남김. 64개 baseline은 인증·로그·세션, 업로드·다운로드·압축·삭제, 백업 증적 성공·거절·오류 경로를 포함함. 인증 200/200문장·52/52분기, 파일 처리 387/388문장·157/158분기, 백업 검증 91/91문장·40/40분기를 기준으로 삼음. 세 핵심 함수는 문장·분기 100%를 요구함.

파일 처리의 유일한 누락은 `format_size`의 471행과 arc465→471임. 마지막 고정 단위 TB에서 조건이 항상 참이라 반환하므로 정상 수치 입력에서 루프 소진과 fallback 반환은 도달 불가함. 해당 코드는 삭제·제외하지 않고 실제 분모와 누락을 보고함. 설정의 `expected_missing_lines`·`expected_missing_branches`는 전체 누락과 정확히 일치해야 하며, 도달 불가 증명의 원본 SHA256이 달라지면 다시 검토하기 전 실패함. 같은 비율로 다른 경로가 빠져도 성공할 수 없음. 제외 pragma·가짜 float/units로 수치를 채우지 않음.

이 결과는 coverage.py가 계산한 문장·줄 간 arc의 도달 증거임. 모든 조건 조합·임의 길이 반복·플랫폼 동작·운영 서비스 통합의 완전 검증을 뜻하지 않음. 비문자 JSON 키 같은 디코더 방어 입력과 OS 오류·시계·토큰 충돌은 좁은 경계 모의 주입으로 구분하며 실제 파일·압축 bytes·상태/원본 보전 assertion을 함께 확인함. 저장소의 나머지 파일은 이 분기 측정 대상에 포함하지 않음.

변이는 인증 제한의 경계 조건, 업로드 크기 제한, 백업 암호화 증적 조건을 대상으로 함. 변경 없는 baseline과 각 변이의 동일 테스트가 먼저 성공해야 하며, 원본을 바꾸지 않은 임시 복사에만 한정된 변이를 적용함. 기존 테스트의 assertion failure로 탐지된 경우만 `killed`로 판정함. 생존·수집/실행 오류·skip·timeout·원본 변경은 각각 실패로 처리하며, 세 변이 탐지가 모든 가능한 결함을 탐지한다는 의미는 아님. 구현자의 실행 결과와 최종 독립 검토자의 원본 대조를 구분해 기록함.

## 3. CI artifact 확인 방법

1. GitHub Actions의 해당 CI 실행을 열고 커밋 또는 PR과 연결된 실행인지 확인함.
2. 서비스별 테스트 job의 상태와 실행 로그를 확인함.
3. 변경 범위 검증 결과에서 `changed_files`, `services`, `required_checks`, `blocked_files`, `unclassified_files`를 확인함.
4. PR 정책 검토 결과와 검증 명령의 종료 상태를 함께 확인함.
5. 확인한 실행 링크, 실행 시각, 결과를 개선 이력에 기록함.

CI artifact가 없거나 로그를 읽을 수 없는 경우 성공으로 처리하지 않고 `검증 불가` 또는 `확인 필요`로 기록함.

### 3.1 보존 및 접근 기준

| 항목 | 기준 | 비고 |
|---|---|---|
| GitHub Actions artifact·로그 보존 기간 | 90일 | 현재 저장소 설정의 최대 허용 기간임 |
| Artifact 열람·다운로드 | 저장소 읽기 권한이 있는 인증 사용자 | GitHub Actions 화면 또는 GitHub CLI 사용 가능함 |
| 장기 보관 대상 | 배포 승인, 장애 분석, 감사에 필요한 최종 증거 | artifact 만료 전에 선별함 |
| 장기 보관 위치 | 저장소 문서 또는 별도 증적 저장소 | GitHub Actions artifact를 영구 보관소로 사용하지 않음 |

- GitHub Actions artifact와 로그는 영구 보관되지 않음.
- 장기 보관이 필요한 증거는 만료 전에 JSON 요약, 실행 링크, 검증 결과를 문서 또는 별도 증적 저장소에 기록함.
- `.superpowers/sdd/`의 이미 추적된 `*-report.md`는 완료 작업의 검증 보고서로 보관할 수 있음. 새 임시 메모·초안은 Git 제외 상태를 유지하며 보관 대상으로 추가하지 않음.

## 4. 결과 분류 기준

| 분류 | 기준 | 처리 |
|---|---|---|
| 성공 | 변경 범위가 분류되고 필요한 서비스 검증과 정책 검토가 통과함 | 결과와 artifact를 기록함 |
| 정책 중단 | 서버 기동·스케줄러 등 금지 영역이 포함되거나 정책상 진행할 수 없음 | 작업을 중단하고 검토를 요청함 |
| 테스트 실패 | 필요한 테스트 또는 CI job이 실행되었으나 실패함 | 원인과 재시도 내역을 기록함 |
| 검증 불가 | 필요한 artifact, 로그 또는 실행 환경을 확보하지 못함 | 성공으로 처리하지 않고 확인을 요청함 |
| 확인 필요 | 분류 결과나 영향 범위를 자동으로 확정할 수 없음 | 변경을 보류하고 담당자 판단을 요청함 |

## 5. 개선 이력 기록 양식

| 항목 | 내용 |
|---|---|
| 날짜/시간 | YYYY-MM-DD HH:MM (KST) |
| 범위 | 변경 파일, 서비스 또는 검토 대상 |
| 실패 유형 | 성공, 정책 중단, 테스트 실패, 검증 불가, 확인 필요 중 선택 |
| 재시도 횟수 | 0~3회 및 각 시도 결과 |
| 조치 | 수행한 수정·검토·확인 요청 |
| 잔여 위험 | 미검증 영역, 운영 영향, 후속 확인 사항 |

### 기록 예시

| 날짜/시간 | 범위 | 실패 유형 | 재시도 횟수 | 조치 | 잔여 위험 |
|---|---|---|---:|---|---|
| YYYY-MM-DD HH:MM (KST) | 변경 파일 또는 서비스 | 분류값 | 0 | 수행한 조치 | 확인 필요 사항 또는 없음 |

## 6. 확인 필요 사항

- PR 정책 검토 결과를 기록하는 위치와 담당자는 팀 운영 기준에 따라 확인 필요함.
- 자동 분류 결과만으로 운영·보안·배포 영향이 확정되지 않는 변경은 담당자 검토 필요함.
- 하네스 원본 입력·출력의 UTF-8 바이트 실측값과 25% 수용 목표 달성 여부는 확인 필요함.
- 실제 토큰 절감률은 동일 조건의 측정 기록이 누적된 뒤 확정 필요함.

## 7. 운영 적용 증적

| 날짜/시간 | 범위 | 실패 유형 | 재시도 횟수 | 조치 | 잔여 위험 |
|---|---|---|---:|---|---|
| 2026-09-15 14:38 | 월간 SRE 통합 점검 N100 적용 | 성공 | 2 | `main` 병합 커밋 기준으로 N100 저장소를 fast-forward 동기화함. 최신 audit runner 이미지를 K3s에 반입하고 preflight·render·validation 수동 Job·월간 수동 Job·기존 SRE Telegram relay·격리 lab 정리·공개 health 3회 HTTP 200을 확인한 뒤 월간 CronJob을 활성화함 | 실제 모델 토큰 측정 기록은 수집하지 않아 토큰 절감률 확인 필요함 |

### 7.1 재현 기준

| 항목 | 결과 |
|---|---|
| 저장소 변경 | PR #200 병합, `main` 커밋 `d27cba8` |
| CI | 정책·보안·서비스별 격리 테스트·K3s 계약·summary 통과 |
| N100 수동 점검 | 종합·Portal health·백업 증적·격리 복구 단계 통과 |
| 자동 실행 | `monthly-sre-audit` 활성화, 기존 분기·validation CronJob suspended 유지 |
| 외부 health | 10초 간격 3회 모두 HTTP 200 |
| 운영 문서 대조 | Portal 1/1 Ready, K3s SRE health audit 통과, 문서에 정의된 Compose 운영 서비스가 실행 중인 상태를 확인함 |
| 제외 범위 | Secret 값, Portal PVC, Caddy, Cloudflare Tunnel ingress, Portal·Compose 서비스 변경 없음 |
