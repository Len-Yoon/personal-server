# CI·로컬 공통 테스트 매트릭스 설계

## 목적

여러 서비스가 동일한 `app` 패키지명을 사용해도 로컬에서 CI와 같은 테스트 범위·Python 버전·모듈 경계로 한 명령 검증할 수 있도록 함.

## 문제 원인

저장소 루트에서 `python3 -m unittest discover`를 실행하면 서로 다른 서비스의 `app` 패키지가 한 Python 프로세스에서 충돌함. CI는 서비스별 Python 버전과 `PYTHONPATH`를 설정해 별도 Job으로 실행하므로 이 명령은 CI의 등가 검증이 아님.

## 설계

| 구성요소 | 책임 |
|---|---|
| `tests/ci_test_matrix.json` | 테스트 그룹명, Python 버전, requirements 경로, 추가 패키지, 단독 `PYTHONPATH`, unittest 명령을 정의하는 단일 기준 자료임 |
| `tests/run_service_tests.py` | JSON을 검증·로딩하고, 로컬에서 각 그룹을 별도 subprocess로 실행함. `--github-matrix`는 GitHub Actions matrix JSON만 출력함 |
| `.github/workflows/ci.yml` | matrix 생성 Job의 JSON 출력으로 테스트 Job을 동적 구성함. 의존성 설치와 Python 설정은 matrix 필드를 사용함 |
| `tests/test_run_service_tests.py`·`tests/test_compose_config.py` | 로컬 실행기·CI workflow·JSON 매트릭스의 동등성과 격리 경계를 검증함 |

## 실행 계약

1. 기본 실행은 CI와 같은 9개 Python 그룹만 수행함.
2. 그룹마다 `python3.<major.minor>`을 명시 실행하고, `PYTHONPATH`는 해당 서비스 경로 또는 저장소 루트로 덮어씀. 기존 `PYTHONPATH`는 상속하지 않음.
3. 그룹 실패 또는 interpreter 부재는 기록하되 나머지 그룹을 계속 실행하고 마지막에 nonzero를 반환함.
4. requirements 설치·네트워크 접근은 로컬 실행기의 범위가 아님. 필요한 의존성이 없으면 해당 그룹은 실패로 보고함.
5. CI 외 Node/browser 확장 테스트는 기본 공통 매트릭스에 넣지 않음.

## 제외 범위

- 서비스의 `app` import 문구와 애플리케이션 코드는 변경하지 않음.
- Secret, 환경 변수 값, 외부 자격 증명을 읽거나 출력하지 않음.
- 운영 배포·K3s·CronJob·N100 설정을 변경하지 않음.

## 성공 기준

- 로컬 `--dry-run`과 CI가 동일한 9개 그룹·명령·Python 버전·PYTHONPATH 계약을 사용함.
- `unittest discover`의 `app` 충돌 대신 서비스별 subprocess 격리로 실행됨.
- CI matrix에 월간 SRE 관련 K3s contract 테스트가 포함됨.
