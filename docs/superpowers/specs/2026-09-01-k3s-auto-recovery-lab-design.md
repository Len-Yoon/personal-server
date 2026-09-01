# K3s Pod 자동복구 실습 설계

## 1. 목적

개인 N100 K3s 환경에서 데이터·외부 공개 경로·기존 서비스를 변경하지 않고, liveness probe 실패 후 Kubernetes가 컨테이너를 자동 재시작하고 Ready 상태로 복귀시키는 과정을 반복 가능하게 검증함.

## 2. 범위

| 구분 | 포함 | 제외 |
| --- | --- | --- |
| 대상 | 전용 `sre-recovery-lab-<run-id>` namespace의 무상태 Deployment 1개 | Portal, Compose 서비스, PVC, SQLite, Caddy, Flux, Secret |
| 장애 주입 | Pod 내부 sentinel 파일 생성으로 liveness probe 실패 유도 | 호스트 종료, Docker/K3s 서비스 중지, 네트워크 차단 |
| 복구 확인 | container restart count 증가, Pod Ready 복귀, liveness failure 이벤트 확인 | 외부 도메인, Caddy·NodePort 검증 |
| 정리 | 해당 run namespace만 삭제하고 부재 확인 | 기존 namespace·image·cluster resource 삭제 |

서버 기동 로직, scheduler, Compose/Caddy 구성, Secret 값은 변경하지 않음.

## 3. 설계

### 3.1 구성 요소

`infra/k8s/tools/sre-pod-recovery-lab.sh`는 N100 운영자가 명시적으로 `--run`을 지정할 때만 실행하는 실습 도구임. run id는 영문 소문자·숫자·하이픈만 허용하며, namespace가 이미 존재하면 실패함.

Deployment는 `busybox:1.36` 컨테이너 하나로 구성함. 컨테이너는 sleep loop를 유지하고 liveness/readiness probe는 `test ! -f /tmp/force-liveness-failure`를 실행함. `kubectl exec`로 sentinel 파일을 만들면 liveness probe가 연속 실패하고 kubelet이 컨테이너를 재시작함. 새 컨테이너의 `/tmp`는 비어 있으므로 probe가 다시 성공함.

### 3.2 검증 흐름

1. 고유 namespace와 Deployment를 생성하고 Pod Ready를 대기함.
2. baseline restart count를 기록함.
3. 현재 Pod 내부에 sentinel 파일을 생성함.
4. restart count가 baseline보다 1 이상이 될 때까지 제한 시간 동안 대기함.
5. 동일 Pod가 Ready 상태로 복귀했는지 확인함.
6. Pod event에서 liveness failure 또는 kubelet restart 근거를 확인함. event 전달 지연으로 누락될 수 있으므로 restart count와 Ready 복귀가 필수 성공 조건이며 event는 기록용 보조 증적임.
7. 성공·실패 모두 정확한 namespace만 삭제하고 부재를 확인함.

### 3.3 실패와 정리

실패 시 기존 서비스에 복구 명령을 보내지 않음. 실습 namespace만 남을 수 있으며, 실행 도구는 명시적인 `--cleanup <run-id>`를 제공함. cleanup은 유효한 run id와 고정 접두사 namespace만 허용하며, 다른 namespace 삭제를 거부함.

`EXIT` trap에 의존하지 않음. 각 실패 분기에서 cleanup을 명시적으로 호출하고, SIGINT/SIGTERM/SIGHUP에는 cleanup 후 종료함. cleanup 실패는 run id를 출력해 운영자가 동일 cleanup 명령을 재시도할 수 있게 함.

## 4. 성공 기준

| 항목 | 통과 기준 |
| --- | --- |
| 격리 | `sre-recovery-lab-<run-id>` 이외 리소스를 만들거나 삭제하지 않음 |
| 자동복구 | sentinel 주입 뒤 restart count가 증가하고 Pod가 Ready로 복귀함 |
| 관찰 | restart count 전후값, Pod 이름, Ready 복귀, event 확인 여부를 출력함 |
| 정리 | namespace 삭제와 부재 확인을 수행함 |
| 안전 경계 | Portal·Compose·Caddy·PVC·Secret·scheduler·서버 기동을 변경하지 않음 |

## 5. 확인 필요 사항

- N100에서 `busybox:1.36` image가 K3s containerd에 없으면, operator가 사전에 image import 또는 pull을 승인해야 함.
- liveness failure event는 K3s event 보존 기간과 타이밍에 따라 보조 증적으로만 사용함.
- 실제 실행은 새 도구의 단위·정적 검토가 끝난 뒤 별도 운영 승인으로 진행함.
