# 7개 앱 HA 릴리스 패키지

운영 Dockerfile·Compose·K3s·CI를 수정하거나 원격 배포를 수행하지 않는 별도 빌더임.
현재 운영 활성화 상태와 무관하게 패키징만 수행하며, 운영 활성화에는 별도 승인된 controller 설정·fencing·복구 검증이 필요함.

```sh
python3 infra/k8s/ha-package/build.py prepare --commit <40자리_검증완료_커밋> --output /tmp/ha-release
python3 infra/k8s/ha-package/build.py verify-source --bundle /tmp/ha-release
python3 infra/k8s/ha-package/build.py build --bundle /tmp/ha-release
python3 infra/k8s/ha-package/build.py verify --bundle /tmp/ha-release
```

| 항목 | 기준 | 비고 |
|---|---|---|
| 원본 | 지정 커밋의 Git blob만 읽음 | 작업 트리 변경은 포함하지 않음 |
| 앱 | Portal·Book·YouTube·Crawler·CarCare·HomeOps·System Agent | 릴리스 ID 하나로 묶음 |
| 베이스 | 원본 Dockerfile의 literal SHA256 고정 Python 3.11/3.12 Alpine | ARG·다단계·root USER·템플릿 변조는 차단함 |
| 원본 보존 | 의존성·CMD·환경·디렉터리·USER를 유지하고 공유 runtime·canonical contract·고정 psycopg를 추가함 | HomeOps Docker socket 권한은 기존 실행 설정을 별도 검토함 |
| 컨텍스트 | 앱 Python·명시된 정적 자산 확장자·requirements·공유 runtime·계약만 포함함 | 숨김 경로·키·DB·data·secret 제외함 |
| 로컬 빌드 | Mac OrbStack의 사용자별 정확한 Unix socket만 허용함 | linux/amd64, 소유 UUID label, 원격 Docker 환경 무시함 |
| 효율 | 원본 의존성 설치 단계 캐시를 재사용함 | all-seven 순차 빌드, 별도 venv fixture 이미지 아님 |
| smoke | 실제 7개 이미지에서 비root·psycopg 버전·runtime import·앱 전체 구문·기본 비활성 상태 검사함 | 격리 네트워크·읽기 전용·권한 제거 실행, DB 통합 검증은 별도임 |
| 무결성 | 원본 파일 manifest·서비스 계약→release ID→생성 release.json 포함 bundle_files→이미지 revision/release label→config digest→layer digest→archive SHA256 연결함 | Docker archive manifest 해시이며 registry manifest digest는 게시 전 미생성임 |
| 배포 계획 | deployment-plan.json은 실행되지 않는 검토 항목과 후속 순서임 | 실제 Secret·mount·apply 명령을 생성하거나 활성화하지 않음 |

출력의 `source-manifest.json`과 검토한 release ID를 함께 보존해야 함. 해시는 무결성 검사용이며 서명·신뢰 배포 채널을 대신하지 않음.
`verify`는 전체 7개 이미지 archive를 요구하며 일부 성공을 전체 성공으로 처리하지 않음.
빌드 실패 시 부분 출력은 검토용으로 보존하고 새 출력 경로에서 다시 준비함. 전역 Docker 정책·운영 이미지·컨테이너를 정리하지 않음.
로컬 빌드 이미지와 archive는 자동 삭제하지 않으며 승인된 반입·검증 또는 명시적인 로컬 정리 이후 제거함.

## 운영 활성화 입력 계약

아래는 파일 형식 설명이며 승인 파일을 발급하거나 운영 활성화를 실행하지 않음.

| 입력 | 필수 값 | 비고 |
|---|---|---|
| 환경 | `APP_ENV=production`, `HA_PG_CONFIG`와 `HA_PG_ACTIVATION`의 절대 파일 경로 | 환경변수만으로 활성화되지 않음 |
| 패키지 고정 식별자 | `/opt/ha/ha_runtime/release.json`의 `source_commit`, `release_id`, `source_files_sha256` | 빌더가 정확한 Git 원본에서 생성함. 소스 트리에는 파일 없음 |
| 설정 파일 | `scope=n100-approved`, 위 식별자 3개, `cluster`, `system_id`, `service`, `node`, `primary_node`, `primary`, `partner` | 필드 추가·누락과 식별자 불일치 차단함 |
| 승인 파일 | 설정과 동일한 식별자·cluster/system_id/service/node/primary_node/primary/partner, `action=activate-ha`, `expires`, `nodes` | scope 필드는 넣지 않음. `expires`는 `Z`로 끝나는 UTC ISO 8601, 현재부터 24시간 이내여야 함 |
| 노드 허용 목록 | `nodes`는 서로 다른 호스트를 가진 `A`, `B` 두 항목 | primary_node에 따른 primary·partner의 host를 정확히 대조함 |
| endpoint | `host`, `port=5432`, `dbname`, `user`, `sslmode=verify-full`, `sslrootcert`, `passfile` | dbname·user는 빈 문자열 금지, password·추가 옵션 금지, 승인 파일과 전체 값 일치 필요 |
| 파일 보호 | 설정·passfile은 실행 UID 소유의 단일 일반 파일, 다른 사용자 권한 없음 | symlink 경로 금지. CA는 root 또는 실행 UID 소유, 다른 사용자의 쓰기 권한 없는 일반 파일 필요 |
| 승인 권한 경계 | activation과 패키지 release.json은 컨테이너 root 소유, 0440 또는 0444의 단일 일반 파일 | 앱은 비root여야 하며 모든 상위 디렉터리도 root 소유·group/other 쓰기 불가여야 함. symlink·hardlink·앱 소유 파일 금지. `/run/secrets` 등 root 관리 경로로 공급함 |

승인 유효기간과 파일 보호는 DB 연결마다 재검증됨. 승인 만료 또는 불일치는 SQLite 전환 없이 연결을 거절함.
운영 승인 파일과 설정 파일은 승인된 별도 절차로 공급해야 하며 본 빌더는 자격 증명이나 운영 승인 파일을 생성하지 않음.
HomeOps Docker 접근은 원래 실행 USER·group·socket 설정에 대한 별도 운영 검토 대상임.
