# 7개 앱·백업 도구 HA 릴리스 패키지

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
| 별도 백업 도구 | `ha-backup-utility` 이미지에 PostgreSQL 17 client·cryptography 50.0.0·psycopg 3.3.8을 포함함 | setuptools 84.0.0·wheel 0.48.0 사용. 기존 앱 이미지에 백업 전용 의존성을 추가하지 않음 |
| 베이스 | 원본 Dockerfile의 literal SHA256 고정 Python 3.11/3.12 Alpine | ARG·다단계·root USER·템플릿 변조는 차단함 |
| OS 보안 하한 | HA 앱 7개와 백업 도구만 `musl`·`musl-utils` ≥ `1.2.6-r3`, `zlib` ≥ `1.3.2-r1`로 업그레이드함 | Alpine 3.24의 기존 서명 검증·저장소를 사용함. 원본 운영 Dockerfile·베이스 digest·pip 요구사항은 유지함 |
| 원본 보존 | 의존성·CMD·환경·디렉터리·USER를 유지하고 공유 runtime·canonical contract·고정 psycopg를 추가함 | HomeOps Docker socket 권한은 기존 실행 설정을 별도 검토함 |
| 컨텍스트 | 앱 Python·명시된 정적 자산 확장자·requirements·공유 runtime·계약만 포함함 | 숨김 경로·키·DB·data·secret 제외함 |
| 로컬 빌드 | Mac OrbStack의 사용자별 정확한 Unix socket만 허용함 | linux/amd64, 소유 UUID label, 원격 Docker 환경 무시함 |
| 효율 | 원본 의존성 설치 단계 캐시를 재사용함 | all-seven 순차 빌드, 별도 venv fixture 이미지 아님 |
| smoke | 실제 앱 7개와 백업 도구 1개에서 비root·고정 버전·Python compilation·proof import·원본 파일 해시·기본 비활성 상태 검사함 | 설치된 APK DB와 `apk version -t`로 OS 보안 하한도 검증함. 백업 도구는 PostgreSQL client 버전·승인 없이 도움말 실행까지 검사함. 격리 네트워크·읽기 전용·권한 제거 실행이며 서비스 HTTP·DB 통합 검증은 별도임 |
| 무결성 | 원본 파일 manifest·서비스 계약→release ID→생성 release.json 포함 bundle_files→이미지 revision/release label→config digest→layer digest→archive SHA256 연결함 | Docker archive manifest 해시이며 registry manifest digest는 게시 전 미생성임 |
| 배포 계획 | deployment-plan.json은 실행되지 않는 검토 항목과 후속 순서임 | 실제 Secret·mount·apply 명령을 생성하거나 활성화하지 않음 |

출력의 `source-manifest.json`과 검토한 release ID를 함께 보존해야 함. 해시는 무결성 검사용이며 서명·신뢰 배포 채널을 대신하지 않음.
`verify`는 앱 7개와 백업 도구 1개의 전체 8개 이미지 archive를 요구하며 일부 성공을 전체 성공으로 처리하지 않음.
빌드 실패 시 부분 출력은 검토용으로 보존하고 새 출력 경로에서 다시 준비함. 전역 Docker 정책·운영 이미지·컨테이너를 정리하지 않음.
로컬 빌드 이미지와 archive는 자동 삭제하지 않으며 승인된 반입·검증 또는 명시적인 로컬 정리 이후 제거함.
OS 버전 하한 충족만으로 취약점 검사가 완료되지는 않음. 새 이미지 archive를 유효한 DB로 별도 검사해야 하며, 기존 이미지·만료 DB의 검사 결과를 새 릴리스 수용 근거로 재사용하지 않음.

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

## 실제 7서비스 동결 자료 이전

`bootstrap_assets/initialize.py --phase migration`은 이미 실행·동기 복제 중인 새 `ha` DB에만 적용하는 별도 명시 실행 경로임. 기존 `catalog` 단계는 빈 카탈로그 준비용이며 실제 데이터 이전이 아님. migration 전에 catalog를 실행하지 않으며, 이미 존재하는 schema·table·function·type·LO·기본 권한 변경이 있으면 거절함. 기존 자료를 삭제하거나 덮어써서 선행 조건을 맞추지 않음.

새 초기화 컨테이너에 운영자가 준비한 사설 frozen 디렉터리를 읽기 전용, 새 receipt 디렉터리를 쓰기 가능으로 추가 연결하고 기존 command를 다음 인수로 명시적으로 대체함. 이 문서는 실행 승인을 발급하거나 컨테이너를 시작하지 않음.

```text
--config /run/ha-init/config.json --phase migration
--migration-manifest /frozen/manifest.json
--migration-sha256 <외부에서 대조한 manifest SHA256>
--migration-receipt /receipts/migration.json
```

| 입력 | 필수 계약 | 비고 |
|---|---|---|
| manifest | schema_version=1, source_commit·cluster·plan_sha256, target_system_id, freeze_receipt, artifacts, services, portal_objects, private_states | Git 릴리스·초기화 설정·실제 새 PG system과 일치해야 함 |
| artifacts | artifact ID → file·sha256·bytes | file은 frozen 디렉터리 안의 상대 경로임. 단일 일반 사설 파일만 허용하며 링크·WAL/SHM/journal sidecar·변경된 파일을 거절함 |
| freeze_receipt | file·sha256·bytes 형식의 별도 사설 JSON | schema_version, capture_id, source_runtime_sha256, writer_graph_sha256, writer_fence_receipt_sha256, writers_closed_at, capture_started_at, capture_finished_at, services, target_system_id, cluster, plan_sha256, artifacts, capture_audit를 포함함 |
| capture_audit | file·sha256·bytes 형식의 별도 사설 JSON | schema_version=1, evidence의 runtime·writer_graph·writer_closure·dispositions 사설 파일 descriptor를 요구함. 파일 bytes/hash·변경 여부를 대조하고 앞의 세 hash를 freeze receipt와 대조함. 외부 관측의 진위를 자체 증명하지 않음 |
| 실제 동결 근거 | 7개 서비스 목록과 모든 artifact의 SHA/bytes가 manifest와 일치해야 함. 종료 관측 ≤ 수집 시작 ≤ 수집 완료의 UTC `Z` 시간을 요구함 | 이 도구는 외부 OS writer 종료·재생성 차단을 직접 검증하지 않음. 수집·권한 검증을 단순 true 값으로 대체하지 않음 |
| services | 고정 7서비스 각각 databases·states 객체 | databases는 component → SQLite artifact ID, states는 상태 key → 정규화 JSON artifact ID임. 누락·소비되지 않은 artifact를 거절함 |
| SQLite | Portal homeops/bookmarks, Book main, YouTube main, CarCare main은 필수임 | Crawler main으로 기존 news SQLite를, HomeOps main으로 기존 기록 DB를 추가 보존할 수 있음. 존재하지 않는 자료를 임의 빈 DB로 대체하지 않음 |
| Portal 상태 | auth_sessions, auth_failures, portfolio, file-namespace | 파일 namespace는 실제 앱의 canonical 검증기를 사용함. 휴지통 포함 전체 blob 참조와 portal_objects의 SHA/크기가 정확히 같아야 함 |
| Book·YouTube 상태 | auth-rate-limit, write-sessions | 메모리에만 있던 인증·쓰기 세션은 명시적으로 무효화하고 전환 후 다시 로그인함. 빈 세션 artifact와 그 처분 근거를 함께 준비하며 실행 중 프로세스 메모리를 수집하지 않음 |
| Crawler·HomeOps 상태 | archive/collection_status, restart_claims | 완료·불확실 상태를 보존함. 상태 정규화는 별도 검증된 수집 단계의 책임임 |
| private_states | car-care-worker → hyundai_tokens → 사설 JSON artifact ID | 앱 상태·다른 서비스·blob·일반 SQLite로 복제하지 않음. `ha_effects.private_state`에만 적재함 |
| provider 정적 설정 | Telegram·Hyundai client 정보·YES24·Executor 자격 연결은 별도 broker 사설 설정으로 준비함 | migration은 이를 생성하거나 앱 환경에 배포하지 않음 |

실제 V1 수집은 WAL을 사용하는 Portal DB의 일관된 SQLite backup, 별도 인증 SQLite, 실제 파일·휴지통과 경로 정보를 포함해야 함. Crawler NEWS DB와 기본 경로가 아닌 Hyundai 토큰 파일도 실제 설정에서 확인해야 함. HomeOps 파일이 조사 경로에 없다는 사실만으로 빈 기록을 선언하지 않으며, 실제 경로 또는 부재를 입증하는 별도 근거가 필요함. 이 입력 이전기는 원본 수집기나 완전성 판정기를 대신하지 않음.

모든 입력 검증 후 idle autocommit 연결의 하나의 outer transaction에서 닫힌 control 준비 → 7서비스 데이터와 blob 이전 → 강제 guard/effects 설치 → 논리 값/hash·원본 불변·닫힌 authority 대조 순으로 처리함. 이미 열린 트랜잭션의 savepoint 실행을 거절함. 일반 앱 역할과 broker 역할의 비밀은 사전 준비된 초기화 설정만 사용하며 새 자격 증명을 생성하지 않음.

receipt는 준비·실패·성공별 새 파일로 남김. commit 응답 또는 성공 receipt 저장이 불확실하면 `UNKNOWN_COMMIT`으로 기록하며 자동 재실행하지 않음. 원본 수집/정규화 완료·기존 writer fencing 검증·공급자 외부 효과 대사·installed authority 발급·writer open·경로 전환을 데이터 이전 PASS로 대신하지 않음.
