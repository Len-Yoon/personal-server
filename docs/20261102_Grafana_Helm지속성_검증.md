# Grafana 대시보드 Helm 지속성 검증

## 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 작성일 | 2026-11-02 | Asia/Seoul |
| 목적 | Helm 업그레이드 렌더에서 기존 대시보드 수정 보존 | 기존 결측 안내·표·기본 필터 |
| 기준 | main 3e47d3d, kube-prometheus-stack 88.6.1 | 실제 운영 원본 해시 정책 재사용 |
| 초기 승인 | 구성 수정·검증·PR/CI·병합 | PR #471 당시 범위. 후속 실제 운영 승인은 아래 별도 기록 |

## 핵심 요약

설치·업그레이드·오프라인 렌더 경로에 동일한 post-renderer를 연결함. Helm 소유 ConfigMap 17개의 지정 data 값만 수정하여 이전 Kubelet·Multi-Cluster·namespace·결측 안내를 유지함. Portal 두 대시보드는 기존 Git YAML로 별도 관리함. 미검토 차트나 원본은 조용히 덮어쓰지 않고 실패 처리함.

## 상세 내용

| 경계 | 처리 | 비고 |
|---|---|---|
| 입력/출력 | Helm manifest stdin→stdout, 메모리 처리 | Secret 포함 전체 입력을 임시 파일·오류 로그에 기록하지 않음 |
| 변경 대상 | 정확한 17개 CM·UID·key·원본/결과 hash | Helm label·release·chart profile 고정 |
| 보존 대상 | 나머지 manifest와 metadata·다른 key·Secret 원문 | 지정 scalar 구간 외 원문 바이트 보존 |
| 안전 실패 | 중복·alias·잘못된 YAML/정책·상한 초과 거부 | 전체 검증 성공 전 stdout 출력 없음, 고정 오류 문구 |
| 기존 live-CAS 계약 | 기본 build_plan의 19개·owner 검증 유지 | Helm 렌더 전용 옵션에서만 17개·렌더 label 검증 |
| 신규 설치 | 기존 --apply의 namespace 존재 거부 유지 | 최초 설치 실패 정리와 기존 release 업그레이드 실패를 구분 |
| 기존 release | 명시 --upgrade, deployed 상태·동일 이름/namespace/chart 확인 | 운영 values 재사용, 고정 post-renderer·wait. 아래 후속 사전 검증 참조 |
| 실패 처리 | 기존 release 삭제·옛 revision 자동 복귀 없음 | 실패는 부분 적용 가능, 상태 확인과 승인된 복구 필요 |

Helm 공식 [post-renderer 계약과 유의사항](https://helm.sh/docs/topics/advanced/#post-rendering)에 따라 모든 변경 주체가 같은 renderer를 사용해야 함. 직접 Helm 호출에서 flag를 생략하거나 과거 revision으로 rollback하면 수동 수정 전 대시보드가 돌아올 수 있음. 고정 차트 88.6.1과 검토한 원본을 지원하며, 다른 차트로 올릴 때는 차트 pin·원본 hash·결과를 함께 재검토함.

`--render-upgrade`는 `helm template --is-upgrade`이며 실제 release의 저장 values나 클러스터 admission을 반영하지 않음. 실제 운영 승인 전에는 저장 values를 메모리에서 합친 최종 desired manifest와 현재 자원 차이, dashboard 19개 원본·복구 기준을 준비해야 함.

후속 실제 운영 승인에 따른 사전 조회에서 저장소 설치 values의 Alertmanager=false와 운영 release의 Alertmanager=true 등 차이를 발견함. 운영 변경 전에 `--upgrade`에서만 Git values 재입력을 제거하여 현재 release config를 유지하도록 보완함. 설치/오프라인 렌더용 values는 유지하며 실제 운영 범위·새 검증·업그레이드/복구 결과는 [운영 검증 기록](20261102_Grafana_Helm운영검증.md)에 구분함. 위 오프라인 실증은 PR #471 시점의 결과이며 그때 수행하지 않은 실제 업그레이드 증거로 확대하지 않음.

## 검토 결과

| 검사 | 결과 | 비고 |
|---|---|---|
| 실제 차트 원본 | 대상 17개 source hash 일치 | 정책 hash 완화 없음 |
| 관련 회귀 | 71개 PASS | 최초 sandbox fixture 쓰기 권한 3건·macOS Bash 빈 인수 1건을 구분하고 실행 권한·인수 호환 보완 후 전체 재검증 |
| 실제 Helm 3 실렌더 | Helm 3.19.0·차트 88.6.1 PASS | 103개 문서·source 17개·after 17개·비대상 byte 보존·멱등성·Helm 및 래퍼 설치/업그레이드 렌더 모두 확인 |
| 전체 9개 검사 | 모두 PASS | K8s 1,555개·4-worker 469.69초, maintenance 731개 포함. 다른 7개 서비스 성공도 별도 기록 |
| 독립 전문 검토 | 구현·실증 원본 선행 대조 완료 | 구현 미참여 gpt-6.1-sol high. 최종 commit·작성 이력 target·receipt·실제 판정은 같은 PR에 고정 기록 |

## 확인 필요 사항

후속 사용자 명시 승인으로 PR #473·#474의 검증 main `14187990be3bbc2a28d9e597dbd5d2e4321ccb4f`을 N100에 동기화하고, 같은 차트 88.6.1의 실제 Helm upgrade를 1회 실행하여 revision 6→7 및 수정본 17개의 Helm 저장 manifest 반영을 확인함. 대시보드 19개·29 UID·원본 표/필터·17개 UP·보호 항목·전후 health·별도 실제 독립 GET/READ 검토를 통과해 실제 원복은 불필요했음. [운영 검증 기록](20261102_Grafana_Helm운영검증.md)에 source 보완·초회 검증기 문제·실제 결과와 승인 완료를 구분함. 아래 표는 PR #471 오프라인 검증 당시 범위이며 후속 실제 운영 결과와 구분함.

| 운영 단계 | 상태 | 비고 |
|---|---|---|
| 저장소 반영 | 이 변경의 기능 PR에서 관리 | 최종 병합 커밋·필수 PR/main CI·Agent Review·보안 결과는 해당 PR 기록 참조 |
| N100 동기화 | 미수행 | 이번 검증은 로컬 오프라인 및 계약 검사 |
| 이미지 반입 | 해당 없음 | 이미지 변경 없음 |
| 수동 운영 검증 | 미수행 | 실제 Helm upgrade·server dry-run 미실행 |
| 자동 실행 활성화 | 해당 없음 | 주기적 재적용·자동복구 추가 없음 |
| 외부 health | 미수행 | 운영 상태 변경 없음; 기존 결과를 이번 배포 증거로 재사용하지 않음 |

## 후속 조치

주 담당은 셸 경로·통합 검증·문서·저장소 반영을 맡고 gpt-6.1-sol medium은 Python 변환기·관련 회귀를 담당함. 별도 gpt-6.1-sol high가 원본·diff·검사와 배포/복구 경계를 독립 검토하며 구현 파일은 수정하지 않음. 파일 소유를 분리하고 같은 파일을 동시에 수정하지 않음. 실제 운영·rollback 결과는 이번 구성 검증과 구분하며, 후속 운영 승인 전 전체 Helm 업그레이드를 실행하지 않음.

공식 checksum을 확인한 Helm 3와 실제 차트를 비공개 로컬 환경에서 사용했으며, 실제 렌더 입력·출력은 공유 로그에 남기지 않고 개수·해시·boolean 증거만 보관함. 차트 archive SHA256은 `d98b1a1a4f286cb6022c1f059edc01a83da31e3f2dd1ee70533384151a4dc354`임. 검증용 Helm·저장소 cache는 운영 설치나 전역 Helm 설정 변경으로 확대하지 않음.
