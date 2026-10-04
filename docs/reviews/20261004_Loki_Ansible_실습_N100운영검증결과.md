# Loki·Ansible 실습 N100 운영 검증 결과

## 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 작성일 | 2026-10-04 | Asia/Seoul 기준 |
| 기준 커밋 | `f66a42a` | [PR #331](https://github.com/Len-Yoon/personal-server/pull/331) 병합 커밋 |
| 기준 자료 | 주 담당의 N100 실행·사후 점검 결과, 저장소 계약 검사 | 비밀값·원시 로그·운영 데이터는 기록하지 않음 |
| 목적 | 샘플 로그 관측과 localhost 자동화의 실제 결과 및 한계를 구분함 | 기존 운영 자원 보호 범위 유지 |

## 핵심 요약

검증된 커밋으로 N100을 동기화하고 승인된 실습 자원만 적용함. Loki 샘플 로그 조회와 Grafana datasource 등록을 확인함. Ansible은 최초 배치·2회차 무변경·롤백·재배치를 실제 실행하여 모두 실패 0건을 확인함. 최초 Loki 로그 조회는 실패했으나 자원 변경 없이 재조회하여 통과함.

## 상세 내용

| 단계 | 결과 | 검증 근거·비고 |
|---|---|---|
| 저장소 병합 | 완료 | PR #331, `f66a42a`. CI·Trivy·독립 검토 승인 후 병합됨 |
| 병합 후 검사 | 통과 | [main CI #37208294841](https://github.com/Len-Yoon/personal-server/actions/runs/37208294841), [Trivy #37208294906](https://github.com/Len-Yoon/personal-server/actions/runs/37208294906) 성공 |
| 안전 자동배포 | 정책상 제외 | [Deploy N100 #37208294853](https://github.com/Len-Yoon/personal-server/actions/runs/37208294853)의 `action=blocked`, `reason=blocked_path`. 이후 승인된 수동 실습 적용을 수행함 |
| N100 동기화 | 완료 | N100 저장소 커밋 `f66a42a` 일치 |
| 이미지 반입·사용 | BusyBox digest pull 완료, Loki·Alloy 실행 이미지 사용 확인 | Loki·Alloy의 새 pull 수행은 별도 확인하지 않음. 기존 운영 서비스 이미지 교체 없음 |
| 수동 검증 | 통과 | 아래 Loki·Ansible 결과 참조 |
| 자동 실행 활성화 | 해당 없음 | 실습에 신규 정기 실행·운영 자동복구를 추가하지 않음 |
| 외부 health | 통과 | Portal 적용 전·후 각 10초 간격 3회 정상 응답 확인 |

| 대상 | 확인 결과 | 비고 |
|---|---|---|
| Loki·Alloy·샘플 앱 | Deployment 3개 모두 `1/1`, 전용 PVC Bound | 실습 전용 namespace 대상 |
| 샘플 로그 조회 | 실제 LogQL 조회 통과 | 최초 1회는 기동 직후 실패, 자원 변경 없는 재조회에서 통과 |
| Grafana datasource | chart 88.6.1 Helm 렌더 통과, live label 대조·UID `loki-lab` 등록 로그·관련 파일 2개 확인 | datasource·dashboard 파일 확인이며 대시보드 화면/API 조회는 미수행 |
| Grafana → Loki | ready 조회 exit 0 | 허용 통신 경로 |
| 샘플 → Loki | DNS 조회 통과, `wget -T 5` exit 1·connection refused | timeout이 아님. Grafana의 동일 URL ready 및 Alloy 수집은 통과했으나 CNI 규칙이 원인인지 추가 검증하지 않음 |
| Ansible syntax-check | site·rollback 2개 통과 | localhost inventory·비승격 실행 |
| Ansible check mode | 통과 | 실제 배치 전 검사 |
| Ansible 최초 배치 | `changed=5`, `failed=0` | loopback 전용 샘플 Compose |
| Ansible 2회차 배치 | `changed=0`, `failed=0` | 같은 입력의 멱등성 확인 |
| Ansible rollback | `changed=2`, `failed=0` | 실습 전용 자원 정리 |
| Ansible 재배치 | `changed=5`, `failed=0`, 최종 HTTP 정상 | 실습 앱을 재배치한 상태로 종료 |
| 기존 monitoring | Deployment 4개·StatefulSet 2개 모두 `1/1` | 기존 관측 자원 준비 상태 유지 |

## 검토 결과

소유권 의미 계약은 정상 label에서 count·sort·label 조건을 개별 평가하도록 보완됨. site·rollback 조건 우회 변이 20/20을 검출했고, maintenance 전체 546건이 통과함. datasource provisioning에 `prune: true`를 추가하고 관련 계약을 검증함. 독립 운영 사후 검토 결과 APPROVE이며 추가 차단 항목 없음. 실습은 기존 운영 앱·백업·Secret·Caddy·Tunnel·scheduler를 변경하는 경로와 분리됨.

## 확인 필요 사항

- Grafana 대시보드 화면 및 API를 통한 실제 로그 표시 조회는 미수행함.
- 24시간 retention의 실제 데이터 삭제와 디스크 사용량 강제 제한은 미검증임. local-path PVC 1Gi 요청은 hard quota를 보장하지 않음.
- Loki 실제 rollback 및 datasource DB 삭제의 운영 리허설은 미수행함. Ansible rollback 성공과 구분함.
- 샘플 Pod에서 Loki 접근 실패는 확인했으나 CNI 규칙에 의한 NetworkPolicy 강제 집행으로 확정하지 않음. 모든 경로 조합도 미검증임.

## 후속 조치

1. 기동 직후 로그 인덱싱 대기를 고려한 검증 도구의 제한된 재시도 개선을 별도 변경으로 수행함. 이번 최초 조회 실패를 삭제하거나 성공으로 재분류하지 않음.
2. Grafana 화면/API 로그 표시와 retention·디스크 경보를 별도 확인함.
3. 운영 서비스 로그 수집 확대·PVC 데이터 삭제·추가 배포는 기존 실습 승인에 포함하지 않음.
