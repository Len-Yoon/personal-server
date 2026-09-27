# A7 NetworkPolicy 격리 시험 설계

## 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 작성일 | 2026-09-27 | Asia/Seoul |
| 기준 자료 | 저장소 A7 계획, K3s·Kubernetes 공식 NetworkPolicy 문서 | 실제 N100 집행 여부는 확인 필요 |
| 목적 | 운영 namespace 변경 전에 임시 namespace에서 정책 집행을 증명함 | N100 실행·운영 정책 적용 제외 |

## 핵심 요약

Flannel VXLAN은 통신 기반이며 K3s의 내장 NetworkPolicy 컨트롤러 활성·집행 상태는 별도 검증이 필요함. 이 도구는 실행자의 명시 `--go`와 현재 context 확인 후 고유 임시 namespace만 생성함. 정책을 운영 namespace에 적용하지 않음.

BusyBox digest는 저장소의 `infra/k8s/sre-audit-automation/quarterly-sre-audit-cronjob.yaml`과 `infra/k8s/tools/sre-pod-recovery-lab.sh`에 있는 고정 참조를 재사용함. 2026-09-27 N100의 `k3s ctr -n k8s.io images ls -q` 읽기 전용 조회에서 동일 digest 참조 1건을 확인함. 실제 smoke 시점의 이미지 존재·아키텍처·실행 성공은 아직 확인되지 않았음. Pod의 `imagePullPolicy: Never`로 실행 중 registry pull을 막음.

## 시험 경계와 성공 기준

| 단계 | 임시 자원·검사 | 성공 기준 |
|---|---|---|
| 사전 | 명시 context, 고정 kubectl 명령, 고유 `np-smoke-*` namespace 이름, 기존 namespace 부재 | context 불일치·이름 충돌 시 생성 전 중단 |
| baseline allow | digest 고정 BusyBox 서버·허용/차단 클라이언트 Pod | 두 클라이언트가 서버 8080에 연결되고 DNS 조회 성공 |
| default deny | 같은 namespace의 세 Pod에 Ingress·Egress 빈 규칙 | 두 클라이언트의 HTTP와 DNS 조회가 실패하고 서버 로컬 응답은 정상 |
| 선택적 allow | 서버 ingress는 허용 클라이언트 8080만, 해당 클라이언트 egress는 서버 8080과 kube-system DNS 53만 | 허용 HTTP·DNS 성공, 차단 클라이언트 HTTP·DNS 실패 |
| 정리 | 자신이 생성한 namespace만 삭제하고 소멸 확인 | 실패·중단에도 정리 시도, 실패 시 이름과 수동 정리 명령 기록 |

NetworkPolicy는 허용 규칙을 합산하며 source egress와 destination ingress가 모두 허용되어야 연결됨. 정책 전파는 비동기이므로 단발 결과 대신 제한된 반복 관찰을 사용함. 네트워크 기능 시험 실패를 서비스 장애로 해석하지 않고 운영 정책 적용을 보류함.

## 보호·확인 필요

- 운영 `default`·`monitoring`·앱 namespace, K3s 전역 재시작, CNI 설정, Caddy·Tunnel, PVC·Secret은 변경하지 않음. 임시 Pod는 서비스 계정 토큰·hostNetwork·PVC·NodePort를 사용하지 않음.
- N100에서 실제 실행하려면 별도 운영 승인을 받은 뒤 독립 운영 사전 검토를 수행함. 실행 전후 Portal과 변경 대상의 외부 health를 각각 10초 간격 3회 확인하고, namespace 삭제 결과를 확인함.
- 도구 검증만으로 운영 서비스 간 최소 통신을 확정하지 않음. ingress·DNS·monitoring scrape·알림 흐름의 실제 의존 관계를 별도 읽기 전용 수집한 다음 운영 정책을 설계함.
- [후보 경계 검토](../../reviews/20260927_NetworkPolicy_후보경계_검토.md)에 N100 읽기 전용 label·Service·port 조사와 적용 금지 템플릿을 기록함. Caddy/NodePort 원본 및 Portal 기타 egress가 확인되기 전에는 운영 정책을 적용할 수 없음.
- K3s 내장 정책 컨트롤러가 비활성이면 API `apply`가 성공해도 deny 단계가 실패해야 함. N100 상태와 그 원인은 현재 확인 필요임.

## 실행 준비와 외부 확인 절차

| 단계 | 명령·판정 | 비고 |
|---|---|---|
| 매니페스트 확인 | `python3 infra/k8s/tools/networkpolicy-smoke.py --render` | 로컬 출력만 수행, cluster 접근 없음 |
| context 확인 | `sudo -n k3s kubectl config current-context` | 실제 실행 대상과 일치해야 함 |
| 이미지 사전 확인 | 승인된 운영 사전점검에서 `sudo -n k3s ctr -n k8s.io images ls -q` 결과에 고정 BusyBox digest 참조가 있는지 확인 | 없으면 smoke 미실행. 검증된 동일 digest의 OCI 이미지를 별도 승인된 반입 절차로 반입·재확인한 뒤 진행; 새로운 digest를 추정하지 않음 |
| 사전 외부 health | Portal `https://len.pe.kr/health`, News `https://news.len.pe.kr/health`, YouTube `https://memo.len.pe.kr/health`, Book `https://books.len.pe.kr/health` 각각 `curl --fail --silent --show-error --max-time 15`로 10초 간격 3회 HTTP 200 확인 | 실험 전 기준선 |
| 격리 시험 | 별도 승인 후 `python3 infra/k8s/tools/networkpolicy-smoke.py --go --context <확인된-context>` | 임시 namespace만 생성함 |
| 사후 확인 | 도구의 `networkpolicy_smoke=PASS`, 임시 namespace 부재, 같은 외부 health 3회 확인 | 한 항목이라도 실패 시 운영 정책 적용 보류 |
| 정리 실패 | 출력된 이름에 한해 `python3 infra/k8s/tools/networkpolicy-smoke.py --cleanup np-smoke-<실행-ID> --context <확인된-context>` | 소유 label이 맞지 않으면 삭제 거부함 |

외부 health와 namespace 소멸은 별도 독립 운영 검토에서 재확인함. HTTP 200은 서비스의 전체 기능 보증이 아니며, 이 실험은 운영 namespace의 NetworkPolicy를 증명하지 않음.

## 참고

- [Kubernetes NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/)
- [K3s Networking Services](https://docs.k3s.io/networking/networking-services)
