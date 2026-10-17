# 로컬 배포 표준화 실습

## 1. 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 작성일 | 2026-10-17 | Asia/Seoul |
| 목적 | 릴리스 식별·사전 점검·앱 교체·정상 확인·1회 롤백을 같은 절차로 재현함 | 기존 Book 환경 사용 |
| 범위 | 실제 Book FastAPI/uvicorn의 로컬 프로세스 배포 | 운영 배포·컨테이너·K3s 변경 없음 |
| 기준 자료 | [설계](superpowers/specs/20261017_배포표준화실습_설계서.md), [검증 결과](reviews/20261017_배포표준화실습_검증결과.md) | 3단계 실습 |

## 2. 핵심 요약

현재 Git 커밋의 Book 앱을 고정 릴리스로 묶고 실제 HTTP 서버를 순차 교체함. 정상 재배포, 잘못된 readiness 경로의 후보 실패와 1회 롤백, 변조 후보의 사전 차단을 자동 검증함. 기존 운영 배포 도구는 수정하지 않음.

모든 릴리스의 앱 payload는 같은 소스임. 릴리스 식별자와 readiness 설정이 다른 재배포 훈련이며 새 앱 기능·이전 버전과의 마이그레이션·컨테이너 이미지·무중단 배포를 증명하지 않음.

## 3. 상세 내용

### 3.1 실행

저장소 루트에서 기존 Book Python 환경과 사용하지 않은 출력 이름으로 실행함. 새 환경은 `tests/ci_test_matrix.json`의 Book 의존성 설치 기준을 따름.

```bash
.venv/book-memo/bin/python book-memo/tools/deployment_standard_lab.py --output /tmp/release-first.json
.venv/book-memo/bin/python book-memo/tools/deployment_standard_lab.py --scenario readiness_failure --output /tmp/release-rollback.json
.venv/book-memo/bin/python book-memo/tools/deployment_standard_lab.py --repeats 3 --output /tmp/release-repeat.json
python3 -m json.tool /tmp/release-first.json
```

기본은 3종 각1회, `--repeats`는1~3회임. `--deadline`은0초 초과~180초이며 기본180초임. 준비 Git 명령·HTTP·IPC에 같은 전체 예산을 적용하고, 별도 Git10초·HTTP5초·IPC12초 상한을 함께 적용함. 종료에는 유예5초와 강제 회수5초를 추가 허용함. 기존 출력 파일·symlink는 덮어쓰지 않음. 외부 URL·DB·PID·Git ref·실행 명령은 입력받지 않음.

### 3.2 표준 절차와 실패 처리

| 단계 | 확인 항목 | 중단·보존 기준 | 비고 |
|---|---|---|---|
| 릴리스 준비 | 현재 커밋의 app·requirements·Dockerfile을 Git blob으로 추출, 파일·설정 SHA256 고정 | 링크·경로 탈출·크기 초과 거부 | 작업 트리·환경 파일 제외 |
| 기준 정상 확인 | 실제 /health·/ready 200, 고정 책·메모 생성 | baseline 불량이면 후보 적용 전 중단 | 임시 DB 전용 |
| 후보 사전 점검 | 독립 보관 descriptor와 manifest·전체 파일·추가/누락을 대조 | 변조면 기존 actor 유지 | `artifact_tamper` |
| 이전 종료 | stop 응답·join·exit0 확인 | 종료 실패면 후보 시작 금지 | 단일 writer 유지 |
| 후보 구동·검증 | 실제 uvicorn 구동·HTTP health·설정 readiness 확인 | 잘못된 readiness 경로는 실제404 | `redeploy` / `readiness_failure` |
| 1회 롤백 | 실패 후보 종료 뒤 baseline artifact 재검증·재구동 | 실패해도 추가 롤백하지 않음 | 알려진 readiness 오류 시나리오 |
| 데이터·기능 확인 | 전체 관련 테이블 논리 snapshot·integrity·고정 내용·버전, 실제 /books/1 읽기 | 불일치면 실패 유지 | DB 자동 복원·downgrade 없음 |
| 종료·정리 | 소유 자식 회수, 임시 디렉터리 삭제와 부재 확인 | 살아 있는 자식·삭제 실패면 valid=false | 무관한 프로세스 유지 |

### 3.3 결과 해석

| 결과 항목 | 의미 | 비고 |
|---|---|---|
| valid | 알려진 실습의 기대 전이·검증·정리가 충족됨 | 후보 배포 성공과 구분 |
| deployment_succeeded | 후보 재배포가 정상·보존 검증까지 통과함 | 정상 재배포만 true |
| rollback_succeeded | 실제 baseline 재구동·readiness·데이터 확인이 통과함 | readiness 시나리오 true, 미시도 null |
| rollback_attempts | 복구 시작 횟수 | 최대1회 |
| starts | 실제 ready handshake를 확인한 actor 수 | 정상2·롤백3·사전차단1 |
| active_release | 최종 검증한 논리적 활성 릴리스 | 실습 종료 시 actor는 모두 중지됨 |
| releases | Git SHA·파일별 digest·payload_digest·release_digest | 이미지 digest와 다름 |
| events | 실제 수행한 단계의 UTC 시각·monotonic 경과 시간 | 누락 단계 성공 채움 없음 |

`readiness_failure`는 **실습 valid=true, 후보 deployment_succeeded=false, rollback_succeeded=true**가 기대 결과임. 롤백 성공을 후보 배포 성공으로 바꾸지 않음. 이 도구의 종료0은 실습 계약 통과를 뜻하며 운영 CD 성공 코드로 재사용하지 않음.

### 3.4 수동 학습 Runbook

실행 전에 변경 대상·릴리스 출처·정상 조건·실패 판정·롤백 대상·데이터 보존 조건을 적음. 출력에서 예상한 순서와 실제 이벤트를 대조하고, 후보를 막은 단계와 정상 릴리스가 유지·복원된 이유를 설명함. `readiness_failure`의 health200·readiness404가 보여 주는 차이, 같은 payload라도 설정 변경으로 배포가 실패할 수 있다는 점을 기록함.

이 CLI는 알려진 실패와 조치를 자동 실행하는 훈련 도구임. 운영 서비스·임의 릴리스·DB로 확장하여 사용하지 않음. 실제 운영 절차는 [N100 배포 안내](n100-github-auto-deploy.md)와 운영별 승인·검증 기준을 따름.

## 4. 검토 결과

Git 실행에는 고정 환경을 사용하여 GIT_DIR/GIT_WORK_TREE·설정 주입으로 출처가 바뀌지 않게 함. 전체 tar 사전 검증, 추출 후 정확한 inventory·해시 검사, 실행 전 바이트코드 쓰기 금지로 릴리스를 보존함. child는 환경·import 경로를 제한한 뒤 실제 앱을 import하며, inherited socket·owner·PID·release digest handshake를 검증함. [원본·검사·독립 검토](reviews/20261017_배포표준화실습_검증결과.md)를 함께 확인함.

## 5. 확인 필요 사항

기존 Python 의존성을 재사용하므로 독립 이미지의 dependency 재현성을 증명하지 않음. 새 코드 버전·DB schema 변경 호환성·실제 운영 배포·외부 health·무중단·트래픽 전환·배포 성능·보안 자격 증명 공급은 미검증임. generic 운영 배포 실행기로 사용하지 않음.

## 6. 후속 조치

앞선 [성능 실습](search-performance-lab.md), [장애 진단·복구 실습](incident-recovery-lab.md)과 함께 성능 관측→장애 증거→릴리스·롤백 절차를 설명하는 포트폴리오 자료로 사용함. 운영 확장은 별도 대상·변경·승인과 실제 증거를 준비한 뒤 수행함.
