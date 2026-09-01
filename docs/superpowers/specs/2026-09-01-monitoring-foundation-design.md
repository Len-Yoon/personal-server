# K3s 내부 관측 플랫폼 설계

## 1. 목적

N100 단일 노드 K3s 환경에서 Prometheus와 Grafana로 서버·클러스터 상태를 시간 그래프로 관찰할 수 있게 함. 외부 인터넷 공개 없이 운영자가 N100 내부에서만 Grafana에 접근하도록 함.

## 2. 범위

포함 범위:

- `monitoring` namespace에 `kube-prometheus-stack` Helm release 설치
- Prometheus, Grafana, kube-state-metrics, node-exporter 기반 지표 수집
- Prometheus 7일 보관 및 native ext4 local-path PVC 사용
- Grafana 내부 전용 접근을 위한 일회성 `kubectl port-forward` 운영 절차
- 설치 전 사전점검, 설치·검증·롤백 절차를 자동화하는 운영자 도구

제외 범위:

- Caddy, 80/443, 도메인, 외부 공개 Ingress 변경
- 기존 Compose 서비스, Portal, 서버 기동, Windows bootstrap, 스케줄러 변경
- Alertmanager·Telegram 알림, 자동 복구 정책, Flux 연결
- 애플리케이션별 커스텀 metrics 및 Secret 값을 Git에 기록하는 작업

## 3. 구성

```text
K3s cluster
├─ monitoring namespace
│  ├─ Prometheus Operator
│  ├─ Prometheus ── local-path PVC (7일 지표 보관)
│  ├─ Grafana ───── local-path PVC (대시보드 설정 보관)
│  ├─ kube-state-metrics
│  └─ node-exporter (노드 자원 지표)
└─ 운영자 터미널
   └─ kubectl port-forward → 127.0.0.1:3000 → Grafana ClusterIP Service
```

`kube-prometheus-stack`을 사용함. 이 chart는 Prometheus Operator, Grafana 대시보드, Prometheus 규칙을 포함하는 표준 Kubernetes 관측 구성임. 설치 시점에는 `helm show chart`로 조회한 chart version을 values 파일에 명시적으로 고정함.

## 4. 접근 및 보안

- Grafana Service는 `ClusterIP`만 사용함. NodePort, LoadBalancer, Ingress를 만들지 않음.
- 운영자는 N100에서 `kubectl port-forward --address 127.0.0.1`로만 Grafana에 연결함.
- Grafana 관리자 비밀번호는 Helm이 생성하는 Kubernetes Secret에만 존재함. Git·`.env`·명령 출력·문서에 기록하지 않음.
- 조회 명령은 Secret 값을 출력하지 않으며, Secret의 존재와 참조 이름만 검증함.
- 외부에서 확인이 필요해지면 새 단계에서 VPN/Tailscale 같은 인증된 사설 접근 방식을 설계함.

## 5. 저장소 및 자원 정책

- Prometheus: local-path PVC 5Gi, retention 7d.
- Grafana: local-path PVC 1Gi.
- 단일 노드의 메모리 여유를 고려해 각 구성요소에 명시적 request/limit을 둠. 실제 값은 사전점검으로 N100 가용 메모리를 확인한 뒤 values 파일에 기록함.
- 저장소는 `/var/lib/rancher/k3s/storage`의 K3s local-path provisioner를 사용함. 기존 Portal 데이터나 `/mnt/c`를 사용하지 않음.

## 6. 설치 흐름

1. 읽기 전용 사전점검: K3s Ready, `local-path` StorageClass, Helm 유무, 가용 메모리·디스크, chart registry 연결 확인.
2. 사전점검이 모두 통과한 경우에만 chart version과 이미지 목록을 출력하고 Helm render를 수행함.
3. 운영자 명시 승인 후 `monitoring` namespace 및 Helm release를 설치함.
4. Prometheus·Grafana PVC Bound, 핵심 Pod Ready, Grafana ClusterIP 존재를 검증함.
5. 일회성 port-forward로 Grafana login 화면 접근을 검증함. 비밀번호 값은 출력하지 않음.
6. 설치 결과·chart version·검증 결과만 증적으로 남김.

## 7. 실패 및 롤백

- 사전점검 실패, render 실패, 이미지 pull 실패, PVC Pending, Pod Ready timeout은 설치 성공으로 처리하지 않음.
- 설치 중 실패하면 해당 Helm release와 `monitoring` namespace 리소스만 대상으로 롤백함. 기존 K3s·Compose·Caddy 리소스는 변경하지 않음.
- 정상 설치 뒤 삭제가 필요하면 별도 `--uninstall` 명령으로 Helm release와 monitoring namespace만 제거함. PVC 삭제 여부는 명령 전에 명시적으로 확인하여 지표 데이터의 의도치 않은 삭제를 막음.

## 8. 검증 기준

- K3s node Ready 및 `local-path` StorageClass 확인됨.
- Prometheus·Grafana PVC가 Bound 상태임.
- Prometheus·Grafana·kube-state-metrics·node-exporter가 Ready 상태임.
- Grafana Service가 ClusterIP이며 Caddy 80/443 및 외부 NodePort를 사용하지 않음.
- `kubectl port-forward`를 통한 localhost Grafana 응답 확인됨.
- Prometheus targets에서 Kubernetes/node 수집 대상이 UP 상태임.
- Grafana 기본 Kubernetes 대시보드가 표시됨.

## 9. 후속 단계

관측 데이터를 안정적으로 확인한 뒤 별도 설계·승인으로 다음을 추가함.

- `sre-health-audit` 실패 알림
- Alertmanager에서 Telegram 알림 전송
- 애플리케이션 `/metrics` endpoint 및 ServiceMonitor
- 경고 발생 후 안전한 범위의 자동 대응

