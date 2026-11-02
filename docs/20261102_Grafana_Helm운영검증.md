# Grafana Helm 운영 업그레이드 검증

## 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 작성일 | 2026-11-02 | Asia/Seoul |
| 승인 | N100 동기화·실제 Helm 업그레이드·문제 시 이전 상태 원복·결과 확인 | 실제 사후 검증 완료, 이번 승인 완료·소진 |
| 운영 source | 14187990be3bbc2a28d9e597dbd5d2e4321ccb4f | PR #473·#474, PR/main CI·보안·전문 소스 검토 통과 |
| 실제 실행 | Helm 3.21.4, kube-prometheus-stack 88.6.1, revision 6→7 | 차트 버전 변경이 아닌 기존 release의 실제 upgrade |

## 핵심 요약

N100을 검증 source로 fast-forward 동기화하고 실제 Helm 업그레이드를 1회 실행하여 revision 7 deployed를 확인함. 기존 수정본 17개의 data가 Helm 저장 manifest에도 반영되었고 Portal 두 개를 포함한 live 대시보드 19개·전체 Grafana UID 29개를 유지함. 자기 검증·별도 실제 GET/READ 독립 사후 검토·원본 화면·전후 health가 통과하여 실제 원복은 불필요했음.

## 상세 내용

| 경계 | 실제 결과 | 비고 |
|---|---|---|
| N100 동기화 | 8d6c996→4b87261→1418799, main fast-forward·tracked clean | 기존 미추적 2개 byte·mode 보존, LF shebang 실측 |
| 최종 server dry-run | 102개 identity 동일·7개 hook 정의 동일·config 동일 | plan SHA256 b64de8aca88521f1f6afc29fbf6072e04993caefee81d91ebb2c8e0c3362ced9 |
| 허용 변경 | Helm 저장 manifest의 CM 17개 단일 data key | 이미 수정된 live 값과 17/17 일치, 비대상 manifest 변경 0 |
| 운영 values | 저장 config hash bff047e58d678519aa23dc2a1d4b923eacc79e27ed04a6ecd392e4f52a81ddcb 유지 | 운영 Alertmanager 설정 보존 |
| live 대시보드 | 19개 UID·원본·metadata·기타 key 보존 | Helm 소유 17개, Portal Git 소유 2개 유지 |
| 보호 항목 | 독립 조회 265개·PVC 8개·앱 Pod 28개 UID/spec/restarts 검사 | 실행 중 4개 Ready. 이전 Helm revision 6 및 신규 7의 정상 저장 기록은 별도 검증 |
| 수집·admission | Git 소유 node-exporter 4개·17개 UP·CA 3개/정책 전체 hash 보존 | 정상 hook 유지·기존 TLS Secret 재사용·완료 hook 확인 |
| 기존 observer 갱신 | 자동차 백업 CM의 status_json 1개 정상 5분 갱신 확인 | 실제 timer 기록·읽기 전용 snapshot 재계산·Git 원본·metadata/RV 증가 직접 대조, 원래 baseline 불변 |
| 실제 원복 | 미실행 | 실제 장애 없음. 과거 revision 및 19개 snapshot/CAS 복구 절차는 fixture 검증 |

### 사전 발견 및 보완

| 항목 | 확인·보완 | 비고 |
|---|---|---|
| 운영 values 차이 | Git 설치 values의 Alertmanager=false와 운영 true 등을 발견하여 --upgrade에서만 Git values 재입력 제거 | PR #473, 설치/오프라인 렌더 values 계약 유지 |
| Windows 체크아웃 | core.autocrlf=true로 Python shebang까지 CRLF가 되어 실제 dry-run 실행 실패 | PR #474, 하위 .gitattributes에서 실행 파일 하나만 LF 고정·실제 checkout 후 실행 회귀 추가 |
| Helm CLI 형식 | CLI가 chart·labels를 생략하여 같은 revision의 기존 저장 기록과 메모리 교차 대조 | 임시 도구 보완, Secret 원문 파일·로그 복제 없음 |
| API 기본값 | desired/live의 기존 정규화 차이 31개 전수 확인 | 승인 live baseline 전체·UID를 보존하고 실제 변경 17개 path만 desired 정확값 검사. 새 child·소실·값 변경은 거부 |
| 외부 HTTP 검사 | 독립 검사 Python 기본 UA=403, 같은 URL의 curl UA Python=200·native curl=200 | 요청 클라이언트 차이로 분류. 최초 FAIL 보존 후 외부 검사만 운영 기준 native curl로 보완하고 전체 독립 사후 읽기 재검증 PASS |

정상 hook은 [certgen create](https://github.com/jkroepke/kube-webhook-certgen/blob/v1.8.8/cmd/create.go)·[patch](https://github.com/jkroepke/kube-webhook-certgen/blob/v1.8.8/cmd/patch.go)의 기존 Secret 재사용·빈 failure-policy 보존 의미를 대조함. 실행 식별은 고유 operation label로 pending/failed까지 구분하고, [Helm label 병합](https://github.com/helm/helm/blob/v3.21.4/pkg/action/validate.go)의 이전 map 복사를 확인함.

## 검토 결과

| 구분 | 결과 | 비고 |
|---|---|---|
| source 자기 검증 | 관련 55개·K8s 1,556개·maintenance 731개 PASS | 서로 겹치는 검사 수를 합산하지 않음. 초기 sandbox 쓰기 실패는 허용된 실행 환경에서 재검증 |
| source 독립 검토 | 고정 5경로 diff·strict provenance PASS | 구현 미참여 gpt-6.1-sol high, PR #474 필수 15개·main CI/보안 성공 |
| 임시 적용/원복 도구 | fixture 49개 자기 검증·별도 독립 재실행 PASS | 실제 102개 transition 교차 검사 및 오답 data·새 child 거부 확인 |
| 독립 적용 전 검토 | actual plan·직접 GET 19개·source/보호 경계 PASS | 원격/journal 쓰기 0, raw/canonical hash 구분 |
| 실제 적용 자기 검증 | revision 7·19개·환경 조건·보호 항목 PASS | 명령 1회, 자동 삭제·자동 rollback 없음 |
| 실제 Grafana API | 전체 29 UID·기본 namespace 8개 유지 | 실제 원본, 응답 대체 없음 |
| Multi-Cluster 원본 화면 | 11/11 패널·두 표 6열·N100 행·상세 링크 PASS | 쿼리 20개, 오류·미관측·No data 0. 두 표 crop 육안 확인 |
| Namespace 원본 화면 | 기본 personal-server·URL monitoring 각각 19/19 패널 PASS | 실제 쿼리 18/25개, 오류·미관측·No data 0 |
| 외부 health | Portal·Book 전후 각각 3회 HTTP 200 | 서비스별 완료 간격 10초 이상, 이전 작업 결과 재사용 없음 |
| Grafana 내부 health | 사후 3회 HTTP 200·DB ok | 10초 이상 간격 |
| 독립 실제 사후 검토 | 직접 GET/READ PASS·API 의미 검사 1,762개 | 19개 raw/canonical·UID/metadata·보호 항목·17개 UP·환경 4개·CA/정책·Portal curl 3회·마지막 revision/19 CM 재조회 확인. 별도 검토자의 화면 육안 검토는 이 판정에 합산하지 않음 |

## 확인 필요 사항

| 항목 | 상태 | 비고 |
|---|---|---|
| 다른 차트 버전 | 신규 검증 필요 | 이번 실제 실증은 고정 88.6.1 범위. 원본 hash·profile·최종 server diff 재검토 필요 |
| 운영 rollback 실증 | 미실행 | 실제 장애 없어 이전 revision으로 되돌리지 않음. 실패 시 writer/hook 종료·소유 revision 확인→Helm rollback→19개 CAS 복원→health 재확인 순서 검토 |
| 필수 운영 검증 | 미해결 차단 없음 | 최초 조회/검증기 문제와 실제 운영 실패를 구분하고 원본 로그 보존 |

## 후속 조치

| 단계·역할 | 상태 | 비고 |
|---|---|---|
| 저장소 병합 | source PR #473·#474 완료 | 실제 적용 후 확정 결과만 별도 문서 PR로 기록, 최종 문서 CI·병합은 해당 PR 기록 참조 |
| N100 동기화 | 운영 source 1418799 완료 | 저장소 병합·실제 Helm 적용과 구분 |
| 이미지 반입 | 해당 없음 | 이미지 버전 변경 없음 |
| 수동 운영 검증 | 자기 검증·별도 실제 독립 검토 PASS | 실제 원복 불필요 |
| 자동 실행 활성화 | 해당 없음 | 주기적 재생성·새 자동복구 없음 |
| 주 담당 | source 보완·실행·자기 검증·원본 화면·문서 | 본인 검증을 독립 검토로 합산하지 않음 |
| 도구 담당 | gpt-6.1-sol high, 임시 helper·fixture만 작성 | 원격 운영 실행·최종 독립 판정 미참여 |
| 전문 운영 검토 | 별도 gpt-6.1-sol high | source/helper 작성 미참여, 실제 plan·직접 GET·사후 읽기 검토. 검토자 작성 read-only 도구의 자체 검증과 구분 |
| 소스 최종 검토 | 별도 gpt-6.1-sol high | 고정 diff·작성 이력·검사·금지 영역 독립 검토 |

차트 archive SHA256은 d98b1a1a4f286cb6022c1f059edc01a83da31e3f2dd1ee70533384151a4dc354, 실행 helper SHA256은 9fb0fe71301ffe599bb43e596e41ee4b22d5eb260a5ec62e845bad9dab95f2f1임. 원시 Helm values·manifest·Secret은 메모리에서만 대조함. 비밀값 없는 대시보드 19개 snapshot은 private journal에 제한 보관하며, 공유 가능한 개수·hash·boolean·원본 화면 crop·실행 결과는 비공개 운영 증거로 보관함. 이번 승인은 완료·소진되며 새 수집·이미지·운영 재실행이나 다른 차트 변경에 재사용하지 않음.
