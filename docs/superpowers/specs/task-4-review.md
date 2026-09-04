# Task 4 보안·운영 독립 검토 결과

## 문서 정보

| 항목 | 내용 |
|---|---|
| 검토 범위 | `13233a5..7b8da2e`의 Task 4 설치기·프리플라이트·운영 문서 |
| 검토 기준 | 설치 권한·release 무결성·credential 보호·원자적 설치·읽기 전용 preflight |
| 검토 일자 | 2026-09-04 |
| 검토 방식 | 정적 코드 검토 및 focused unittest·shell syntax·diff 검사 |

## P0

해당 없음.

## P1

| No | 위치 | 검토 결과 | 근거 | 영향 |
|---:|---|---|---|---|
| 1 | `infra/k8s/tools/install-transition-runner.sh:10,44-45` / `infra/k8s/transition-runner/systemd/personal-server-transition.service:13` / `infra/k8s/transition-runner/runner/personal-server-transition-runner:13` | 설치 대상 실행 파일·validator 경로와 systemd unit·runner가 참조하는 경로가 다름. | 설치기는 `/usr/local/libexec/personal-server-transition/personal-server-transition-runner` 및 동 디렉터리의 validator를 설치하나, unit과 runner는 `/usr/local/libexec/personal-server-transition-runner` 및 `/usr/local/libexec/personal-server-transition-policy-validator`를 참조함. | 설치 후 unit이 `ExecStart` 파일을 찾지 못해 기동 불가하며, runner가 실행되더라도 validator를 찾지 못해 실패함. |
| 2 | `infra/k8s/tools/install-transition-runner.sh:27-29,44-51` | `--release-digest`가 runner 파일만 인증하고 root 설치되는 policy, validator, systemd unit은 인증하지 않음. | digest 비교 입력은 `SOURCE_RUNNER`만 사용함. 그러나 validator는 root runner가 실행하고, systemd unit은 root `ExecStart`를 결정함. | 설치 전 source checkout이 변조되면 승인된 runner digest를 유지한 채 root runtime policy/validator/unit을 교체할 수 있음. release manifest 전체의 digest 또는 서명된 bundle 검증이 필요함. |
| 3 | `infra/k8s/tools/transition-runner-preflight.sh:33-35` | preflight가 source repository의 Python validator를 현재 권한으로 실행하므로 읽기 전용·비밀 비접근 계약을 보장하지 못함. | `python3 "$VALIDATOR" "$POLICY"`가 실행되며, `$VALIDATOR`는 repository 경로임. credential은 root-owned `0600`이므로 실제 상태를 확인하려는 preflight가 root로 실행될 경우 변조된 validator가 credential을 읽거나 외부 부작용을 낼 수 있음. | 문서의 “읽기 전용, credential 값을 읽거나 출력하지 않음” 보장이 깨짐. preflight는 실행 파일을 호출하지 않는 검증으로 바꾸거나, 검증된 설치 artifact만 제한 권한으로 호출해야 함. |
| 4 | `infra/k8s/tools/transition-runner-preflight.sh:27,29,35,49` | PASS/FAIL 인자 전달 문법 오류로 정상 조건도 `status=status=PASS`로 기록되어 항상 FAIL 처리됨. | 실제 실행 결과에 `check=artifact status=status=PASS`, `check=release_digest status=status=PASS`, `check=policy status=status=PASS`가 확인됨. `check` 함수는 두 번째 인자가 정확히 `PASS`일 때만 통과 처리함. | 유효한 설치 사전검증이 완료되지 않으며 운영자는 정상 상태를 확인할 수 없음. 성공 경로를 검증하는 테스트도 부재함. |
| 5 | `infra/k8s/tools/install-transition-runner.sh:20-26,31-36,39-54` | credential directory와 설치 parent directory의 신뢰 경계가 불완전함. | credential directory는 directory·non-symlink 여부만 확인하고 소유자/권한을 검사하지 않으며, credential 검사와 `install` 사이에 재검증도 없음. 설치 parent는 `root:*`만 확인하므로 group/other write 권한을 허용함. | 사용자 쓰기 가능한 directory에서 credential source를 교체하거나 staging/release target을 교란할 수 있음. 모든 ancestor에 root 소유 및 group/other write 금지를 적용하고, credential 복사 시 TOCTOU 방지 검증이 필요함. |
| 6 | `infra/k8s/tools/install-transition-runner.sh:35-36,52-54` | native ext4 및 원자적 release 설치가 전체 target에 대해 보장되지 않음. | ext4 검사는 `/usr/local`만 대상으로 함. policy·credential과 unit은 `/etc` 및 `/etc/systemd`에 설치됨. 또한 세 target을 순차 `mv`하며 rollback이 없음. 첫 번째 이동 뒤 다음 이동이 실패하거나 중단되면 부분 설치가 남고, `:37`의 existing-target 거부 때문에 재실행으로 복구할 수 없음. | `/etc`가 별도 non-ext4 mount인 환경에서 신뢰 경계가 깨질 수 있고, 부분 설치 시 실패-closed 상태와 수동 복구 절차가 없음. 모든 target filesystem 검증 및 rollback 가능한 release 디렉터리/단일 activation pointer가 필요함. |

## 검증 결과

| 항목 | 결과 |
|---|---|
| focused unittest (policy/artifact/installer 도구) | 통과 (16건) |
| shell syntax 검사 | 통과 |
| `git diff --check 13233a5 7b8da2e` | 통과 |
| preflight 실제 실행 | 실패. `status=status=PASS` 출력으로 P1-4 재현됨. |
