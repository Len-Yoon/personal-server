# NetworkPolicy 후보 경계 검토

## 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 작성일 | 2026-09-27 | Asia/Seoul |
| 기준 자료 | 저장소 K3s 매니페스트·Portal cutover·monitoring 설정, Kubernetes·K3s 공식 문서 | N100 실측은 미수행 |
| 목적 | 운영 정책 적용 전 최소 허용 경계와 검증 게이트 정의 | 후보 템플릿은 미적용 |

## 핵심 요약

`infra/k8s/networkpolicy/portal-allowlist.yaml.tmpl`은 검토용 후보임. namespace의 `__PORTAL_NAMESPACE__` 표식 때문에 그대로 적용할 수 없음. **NetworkPolicy는 default-deny가 없어도 선택된 Pod의 해당 방향을 격리함.** 이 초안은 Caddy ingress와 Portal의 다른 egress가 누락되어 있으므로 적용 시 장애 위험이 있음. 임시 namespace smoke에서 CNI 정책 집행이 입증되지 않거나 어떤 운영 probe라도 실패하면 후보 정책은 **미적용**으로 유지함. N100의 실제 트래픽 원본은 확인 필요임.

2026-09-27 N100에서 읽기 전용으로 확인한 값: `personal-server` Portal Pod·Service selector는 `app.kubernetes.io/name=portal-web`, Service는 NodePort 30080 → TCP 8000임. `monitoring` Prometheus Pod는 `app.kubernetes.io/name=prometheus`, `operator.prometheus.io/name=personal-server-monitoring-prometheus`이며, `kube-system` CoreDNS Pod와 `kube-dns` Service selector는 `k8s-app=kube-dns`, DNS 포트는 UDP/TCP 53임. 전체 NetworkPolicy 목록은 0건으로 조회됨. label·endpoint·정책 상태는 실제 적용 직전에 다시 조회 필요함.

## 통신 경계와 후보 규칙

| 흐름 | 저장소에서 확인한 계약 | 후보 allow 경계 | 적용 전 확인 필요 |
|---|---|---|---|
| Caddy → Portal | Compose Caddy가 `host.docker.internal:30080` NodePort로 전달하며 Portal 서비스는 8000으로 연결됨 | Portal 8000 ingress가 필요함. NodePort의 Pod 관측 원본 IP/CIDR을 모르는 상태이므로 규칙을 아직 작성하지 않음 | N100에서 원본 보존/SNAT/host 트래픽의 NetworkPolicy 처리, Caddy 내부·외부 health probe |
| Prometheus → Portal metrics | `ServiceMonitor`는 `personal-server` namespace의 Portal 서비스 `/internal/metrics`를 30초마다 scrape함. N100 Prometheus label 확인함 | Portal 8000 ingress를 확인된 Prometheus Pod label과 `monitoring` namespace로 제한하는 템플릿 | scrape target `UP`, 인증 성공, metrics 응답, 실시간 label 재확인 |
| Portal → DNS | N100 `kube-dns` Service와 CoreDNS selector `k8s-app=kube-dns`, UDP/TCP 53 확인함 | DNS Pod만 TCP/UDP 53으로 허용하는 템플릿 | 실제 Portal DNS 조회 성공 및 라우팅 확인. DNS가 NodeLocal/호스트 경유면 템플릿 재설계 필요 |
| monitoring 내부·외부 | Prometheus/Grafana/relay/SLO Job 등 리소스가 monitoring에 있고 relay는 Alertmanager webhook을 받음 | 현 단계에서 monitoring namespace default-deny 후보 없음 | scrape 전체 target, Alertmanager→relay, relay 외부 알림, Grafana 조회, SLO 수집 경로별 출발지·도착지·포트 |
| 기타 Portal egress | Portal 기능 및 인증·뉴스 등 외부/내부 의존성은 이 문서로 확정되지 않음 | Portal egress default-deny 후보 없음 | 운영 로그·매니페스트·기능 probe로 필요한 목적지를 먼저 식별 |

NetworkPolicy 허용은 누적되며, source egress와 destination ingress가 모두 허용되어야 Pod 간 연결됨. NodePort/호스트 경유 패킷의 원본과 정책 집행은 CNI 및 SNAT 경로에 따라 달라질 수 있어 저장소의 Caddy 주소만으로 `ipBlock`을 확정하지 않음. 외부 트래픽·hostNetwork 트래픽이 정책으로 완전히 제어된다고 가정하지 않음. **이 템플릿의 두 정책은 각각 Portal ingress/egress를 격리하므로 현재 적용 금지**임.

## 정책 전후 판정 게이트

| 순서 | 필수 증거 | 실패 시 처리 |
|---|---|---|
| 1. 읽기 전용 사전 조사 | 현재 context·CNI 설정·NetworkPolicy 컨트롤러 상태, 기존 정책 0건 여부, Portal/monitoring Pod·Service·Endpoint·label, NodePort 경로와 DNS 구현, 고정 BusyBox digest의 N100 containerd 존재 확인 | 후보 정책 미적용. 이미지가 없으면 승인된 이미지 반입·digest 재확인 후 smoke 진행 |
| 2. 외부 기준선 | Portal과 변경 대상 외부 health를 10초 간격 3회 HTTP 200 확인. Caddy 컨테이너→Portal NodePort `/health`, Portal metrics target `UP`, DNS·relay·SLO 기존 결과 기록 | 후보 정책 미적용 |
| 3. 임시 namespace 집행 시험 | A7 smoke에서 baseline allow → default deny의 HTTP·DNS 실패 → 선택적 allow의 HTTP·DNS 복구 → 소유 namespace cleanup 전부 `PASS` | CNI 강제 미입증으로 후보 정책 미적용. 정책 API 수락만으로 통과 처리 금지 |
| 4. 후보 검토 | 실제 관측 원본/목적지·Pod label을 채운 정책, 롤백 방법, 독립 운영 검토 및 별도 운영 배포 승인 | 후보 정책 미적용 |
| 5. 별도 승인된 운영 변경 전후 | 정책별 단계 적용, Caddy/NodePort·Portal 외부 health 3회, metrics `UP`, DNS·monitoring·알림 경로와 기능 probe, 예상 차단 probe 확인 | 적용 중단·해당 정책 제거 후 동일 probe 재확인 |

이 변경에는 4·5단계 실행이 포함되지 않음. 운영 namespace의 default-deny, Portal/Caddy/K3s 재시작, PVC·Secret·Tunnel 설정 변경도 포함되지 않음. 임시 smoke조차 N100에서 실행하지 않음.

## 확인 필요 사항

- N100 K3s NetworkPolicy 컨트롤러가 실제 집행하는지 확인 필요.
- Caddy→NodePort→Portal Pod 구간에서 정책이 관측하는 원본과 host 트래픽 처리 확인 필요.
- Prometheus와 DNS Pod의 실제 label 및 다른 정책과의 합성 결과 확인 필요.
- Portal 전체 egress와 monitoring 내 알림·SLO 흐름의 최소 허용 목록 확인 필요.

## 후속 조치

별도 운영 승인 후 외부 기준선과 임시 smoke를 먼저 실행함. 모든 게이트를 통과한 경우에만 실제 트래픽 관측을 바탕으로 템플릿을 완성하고, 정책 단위로 승인·적용·검증·롤백함.

## 참고

- [Kubernetes NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/)
- [K3s Networking Services](https://docs.k3s.io/networking/networking-services)
