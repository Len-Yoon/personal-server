# Grafana Helm 운영 업그레이드 검증

## 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 작성일 | 2026-11-02 | Asia/Seoul |
| 승인 | N100 동기화·실제 Helm 업그레이드·문제 시 이전 상태 원복·결과 확인 | 이번 사용자 명시 승인 |
| 기준 | 기존 chart 88.6.1, revision 6 deployed | 원시 values·manifest·Secret은 메모리에서만 대조 |

## 핵심 요약

실제 운영 전 조회에서 Git 설치 values와 운영 release의 차이를 확인하여 적용을 보류함. 운영 Alertmanager는 enabled=true·replica 1인데 Git values는 false이므로, 기존 업그레이드 옵션을 그대로 실행하면 정상 알림 구성을 제거할 수 있음. `--upgrade`에서만 설치 values 재입력을 제거하고 저장 release config를 재사용하도록 보완함. 실제 Git 동기화와 Helm 쓰기는 이 사전 조회 단계에서 수행하지 않음.

이후 PR #473의 검증 main `4b8726132c9cd9a71d7ccda5e0cd24b5ac55d40c`으로 N100 fast-forward 동기화를 완료하고 기존 미추적 파일 2개의 byte·mode를 보존함. revision 6·대시보드 19개·102개 manifest 자원·hook 7개의 snapshot을 완료했으나 최종 dry-run 실행이 실패함. N100의 `core.autocrlf=true` 체크아웃에서 Python post-renderer의 shebang까지 CRLF가 된 것을 확인하여, 도구 디렉터리의 `.gitattributes`로 해당 실행 파일 하나만 LF를 고정하고 실제 Git 체크아웃 후 실행 회귀를 추가함. 실제 Helm 업그레이드 쓰기는 아직 수행하지 않음.

## 상세 내용

| 경계 | 처리 | 비고 |
|---|---|---|
| 현재 상태 | Git 4b87261 clean·기존 미추적 파일 2개 보존·Helm revision 6 deployed | LF 보완의 PR/main CI 후 새 source로 fast-forward 예정 |
| values 차이 | Alertmanager true→false, datasource 선택·retentionSize 추가 차이 | 값은 공개 가능한 bool만 기록, 그 외 원문 미저장 |
| 보완 | --upgrade는 --reuse-values만 사용 | --apply/오프라인 렌더는 기존 설치 values 유지 |
| 적용 계획 | 실제 server dry-run의 config·manifest·hooks와 live 자원 직접 대조 | 미검토 서비스/Secret/PVC/이미지·수집 변경 차단 |
| 원복 기준 | 기존 Helm revision·현재 대시보드 19개 별도 snapshot | 이전 revision이 수동 수정 전일 수 있으므로 CAS 복구 병행 |
| 실행 식별 | 정확한 source·archive SHA·desired digest·고유 description/operation label | pending/failed에서는 description이 바뀔 수 있으므로 label로 소유 확인. 불완전 응답은 실제 revision/상태부터 조회 |

## 검토 결과

| 구분 | 상태 | 비고 |
|---|---|---|
| 사전 조회 | 17개 UP·Grafana DB 정상 | 운영 쓰기 0 |
| 소스 보완 회귀 | 관련 73개·K8s 1,555개·maintenance 731개 PASS | 변경 영향 없는 7개 서비스는 source15bbfc9의 동일 소스 성공 결과 재사용. 필수 PR/main CI는 그대로 수행 |
| 사전 server dry-run | config hash 동일·102개 identity 유지·hook 정의 차이 0 | 저장 manifest 차이는 17개 단일 data key이며 현재 live의 수정본과 모두 일치. 최종 source/plan은 실행 직전 다시 대조 |
| 소스 독립 검토 | PR #473 최종 고정 diff PASS·PR/main CI 통과·병합 완료 | LF 보완의 고정 커밋·전문 검토는 별도 수행 |
| 임시 적용/원복 도구 | fixture 41개·별도 전문 재검토 PASS | 실제 Helm CLI가 생략한 chart/labels는 같은 revision의 저장 기록과 메모리에서 교차 대조하도록 보완. 새 source·최종 실제 plan 실행 판정은 별도 |

## 확인 필요 사항

Helm admission hook 7개와 실제 최종 manifest 전체 차이를 확인해야 함. hook·CRD·Service·Secret·PVC와 Git 소유 node-exporter 4개를 대시보드 17개 차이와 함께 검사함. 과거 revision 자동 복귀만으로 현재 대시보드가 복구됐다고 판단하지 않음. 운영 값이 다른 상태에서 새 설치 values를 덮어쓰지 않음.

## 후속 조치

주 담당은 source 보완·실제 사전 조회·실행·자기 검증·문서를 맡음. 별도 gpt-6.1-sol high는 임시 적용/복구 도구와 fixture만 작성하고 원격 실행은 하지 않음. 구현 미참여 gpt-6.1-sol high는 source·도구·실제 diff·실제 사후 결과를 독립 검토함. 같은 파일을 동시에 수정하지 않음. 새 source PR/main CI·고정 archive·보호 기준·복구 절차가 통과한 뒤 승인된 실제 운영 단계를 진행하고 결과를 확정 기록함.
