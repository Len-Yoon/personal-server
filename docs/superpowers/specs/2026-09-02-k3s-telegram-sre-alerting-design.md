# K3s Telegram SRE 알림 1차 설계

## 1. 목적

N100 단일 노드 K3s 환경의 상태를 Telegram에서 필요할 때 조회하고, K3s 장애와 자동 복구 결과를 Telegram으로 수신할 수 있게 함. Grafana는 N100 내부의 수동 관찰 도구로 유지하며, 상태 확인을 위해 Grafana 접속을 요구하지 않음.

## 2. 범위

포함 범위:

- Telegram `/상태` 명령에 K3s 상태 요약을 응답함.
- Prometheus 경고를 Telegram으로 전송하고, Alertmanager resolved 알림으로 복구 상태를 전송함.
- K3s의 기존 자동 복구 결과(Pod 재시작, Deployment 회복)를 관찰·보고함.
- 알림 대상은 K3s node, `monitoring`과 `personal-server` namespace의 Pod·Deployment·PVC, Prometheus scrape target으로 한정함.

제외 범위:

- Compose 서비스 자동 재시작, 기존 Compose 서비스 설정, Portal, Caddy, 외부 Ingress·NodePort·LoadBalancer 변경.
- 서버 기동 스크립트, Windows bootstrap, 기존 스케줄러 변경.
- Grafana 외부 공개, Telegram webhook을 위한 인터넷 공개 endpoint.
- 비밀값을 Git·문서·명령 출력에 기록하는 작업.

## 3. 구성

```text
Telegram 사용자
  ├─ /상태 ───────────────→ sre-telegram-relay
  │                            └─ 읽기 전용 K3s API + Prometheus 조회
  └─ 장애·복구 메시지 ←──── Alertmanager ← Prometheus 규칙
                                  └─ ClusterIP webhook → sre-telegram-relay
```

`sre-telegram-relay`는 `monitoring` namespace의 단일 복제본 K3s Deployment로 운영함. Telegram 수신은 외부 공개 webhook 대신 outbound long polling만 사용함. Alertmanager는 같은 cluster의 ClusterIP Service로만 relay에 전달함. 외부에서 접근 가능한 listener를 만들지 않음.

## 4. Telegram 명령 계약

초기 명령은 `/상태` 하나만 제공함.

- 허용된 Telegram chat ID에서 온 정확한 `/상태` 명령만 처리함.
- 응답에는 node Ready, namespace별 Ready/전체 Pod 수, unavailable Deployment 수, Bound/전체 PVC 수, Prometheus UP/전체 target 수를 포함함.
- 실패 원인은 `K3s API 조회 실패`, `Prometheus 조회 실패`, `일부 대상 비정상`처럼 값·경로·비밀정보 없이 제한함.
- 허용되지 않은 chat ID와 지원하지 않는 명령에는 상태·비밀값을 반환하지 않음.

## 5. 경고 및 복구 계약

1차 경고는 아래 네 종류만 사용함.

| 경고 | 발생 조건 | 최초 알림 | 복구 알림 |
|---|---|---|---|
| Pod 재시작 급증 | 15분 동안 컨테이너 재시작 3회 초과 | firing | 재시작 증가가 멈춘 뒤 resolved |
| Deployment 미가용 | desired replica가 10분 동안 available replica보다 큼 | firing | replica 회복 뒤 resolved |
| PVC Pending | PVC가 10분 동안 Bound가 아님 | firing | Bound 전환 뒤 resolved |
| Prometheus target down | scrape target이 5분 동안 UP이 아님 | firing | target UP 전환 뒤 resolved |

Alertmanager는 동일 경고를 group하고, 기본 반복 간격은 4시간으로 설정함. `send_resolved: true`를 사용해 복구 메시지를 보냄. relay는 자동 재시작 명령을 실행하지 않음. K3s가 수행한 기존 자동 복구의 결과만 관찰·보고함.

### 5.1 Alertmanager 설정 소유권

Telegram SRE 경고용 Alertmanager 설정은 N100에서만 생성하는 **SRE 전용 고정 템플릿**으로 관리함. 임의 Alertmanager route tree의 부분 해석이나 문자열 기반 검증은 사용하지 않음.

- 템플릿은 root route와 단일 `sre_telegram="true"` child route, `sre-telegram-relay` receiver만 허용함.
- child route에는 `group_by`, `repeat_interval: 4h`, `send_resolved: true`, relay ClusterIP webhook URL, bearer credential file을 고정함.
- bearer 값은 N100 Secret seed 과정에서만 삽입하며 Git·문서·명령 출력·로그에 기록하지 않음.
- 운영자는 Secret 생성 전에 권한 0600 임시 파일에서 `amtool check-config`와 고정 템플릿 구조 검증을 실행함. 검증 실패 또는 도구 부재 시 설치를 중단함.
- 템플릿 외 route를 병합하거나 허용하는 요구는 별도 설계가 필요함. 1차 범위에서는 템플릿 외 route를 fail-closed로 거부함.

## 6. 보안 및 비밀값

- Telegram bot token, 허용 chat ID, Alertmanager→relay 인증 토큰은 N100에서 Kubernetes Secret으로만 생성함.
- Git에는 Secret 이름·필수 key 계약만 기록하며 값은 기록하지 않음.
- relay는 Alertmanager webhook에 인증 토큰이 없으면 요청을 거부함.
- relay Service는 ClusterIP만 사용하고, NetworkPolicy를 적용할 수 있는 환경이면 Alertmanager와 relay의 필요한 통신만 허용함.
- `/상태`는 읽기 전용 최소 RBAC만 사용하며, Secret·Pod exec·Pod delete·Deployment patch 권한을 받지 않음.
- Alertmanager Secret의 Kubernetes `.data`는 설치·검증 도구에서 읽거나 출력하지 않음. 검증된 N100-local 임시 파일만 입력으로 사용하며 seed 뒤 즉시 폐기함.

## 7. 저장과 중복 방지

- Telegram long polling offset은 relay 전용 ConfigMap에 기록함.
- relay ServiceAccount에는 해당 ConfigMap과 `/상태`에 필요한 읽기 전용 리소스만 허용함.
- relay 재시작 뒤에는 마지막 처리 offset부터 계속 수신함. 같은 Telegram update를 반복 처리하지 않음.
- Alertmanager alert fingerprint와 상태를 짧게 보관해 중복 firing 메시지를 억제하되, Alertmanager의 group/repeat 정책을 우선함.

## 8. 실패 처리

- Telegram API 전송 실패는 제한된 재시도 뒤 로그·자체 health 실패로 남기며, K3s 리소스를 변경하지 않음.
- K3s API 또는 Prometheus 조회 실패는 `/상태` 응답에 명시하되 추정 상태를 정상으로 표시하지 않음.
- relay가 비정상이면 Pod 재시작은 K3s에 맡기고, Alertmanager 경고를 수신하지 못한 기간은 사후 복구 알림으로 보장하지 않음.
- Alertmanager 또는 relay 설치 실패 시 기존 Prometheus·Grafana·Compose 동작은 변경하지 않음.

## 9. 적용 및 검증

1. manifest·RBAC·경고 규칙·Secret key 계약을 정적 검증함.
2. Secret 값 없이 render와 릴레이 단위 테스트를 통과시킴.
3. N100에서 운영자가 Secret을 수동 생성한 뒤 ClusterIP·Pod Ready·최소 권한을 확인함.
3.1. Alertmanager는 SRE 전용 고정 템플릿을 N100-local 임시 파일로 검증한 뒤에만 Secret으로 seed함. 템플릿 외 route 또는 검증 불가 상태는 설치 전 실패 처리함.
4. 허용 chat의 `/상태` 요청이 비밀값 없이 요약을 반환하는지 확인함.
5. 격리된 test alert로 firing과 resolved Telegram 메시지를 각각 한 번 검증함. 실제 장애를 만들거나 Compose·서버 기동·스케줄러를 변경하지 않음.
6. 적용 후 Grafana, Prometheus, 기존 K3s 모니터링 검증이 계속 통과하는지 확인함.

## 10. 성공 기준

- Telegram `/상태`가 K3s 요약을 반환함.
- 허용되지 않은 chat에서 상태가 노출되지 않음.
- 네 가지 경고가 firing과 resolved 상태를 각각 Telegram에 전송함.
- K3s의 자동 복구를 막거나 Compose·서버 기동·스케줄러를 수정하지 않음.
- Grafana와 Prometheus가 ClusterIP 내부 전용으로 유지됨.

## 11. 후속 단계

- Compose 상태의 읽기 전용 요약을 별도 설계로 추가함.
- 안전한 범위의 runbook 링크·수동 승인 기반 대응을 추가함.
- 필요할 때만 VPN 내부 Grafana 상시 접근을 별도 설계함.
