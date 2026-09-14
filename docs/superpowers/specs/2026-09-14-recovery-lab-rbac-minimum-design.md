# Recovery Lab RBAC 최소화 설계

## 목적

분기 SRE 감사의 테스트용 Pod 복구 검증은 유지하면서, `pods/exec` 생성 권한을 제거함.

## 범위

- `sre-recovery-lab` 네임스페이스의 `sre-pod-recovery` Deployment만 변경함.
- 감사 Runner가 특정 ConfigMap 하나의 `trigger` 값만 읽고 수정하도록 변경함.
- Portal, K3s 운영 워크로드, Secret, PVC, Caddy 및 Telegram 설정은 변경하지 않음.

## 설계

`sre-pod-recovery-trigger` ConfigMap은 기본값 `trigger: "false"`를 제공함. Deployment는 이를 읽기 전용으로 마운트하고, 컨테이너 시작 시 `/tmp/healthy`를 생성함. 실행 중 `trigger`가 `true`이면 `emptyDir`에 남는 단발성 마커를 먼저 기록한 뒤 health 파일을 제거함. liveness probe가 컨테이너를 재시작하고, 재시작된 컨테이너는 단발성 마커를 인식해 health 파일을 다시 만들고 Ready 상태가 됨.

Runner는 시작 전 신호를 `false`로 초기화하고, 기준 Pod 스냅샷을 얻은 뒤 `true`로 전환함. 같은 Pod UID에서 restart count 증가와 container ID 변경, Ready 복귀를 확인한 뒤 종료 경로에서 신호를 `false`로 복구하고 Deployment를 0으로 축소함. 어떤 단계가 실패하더라도 정리 경로는 신호 복구와 scale down을 시도하며, 정리 실패는 감사 실패로 기록함.

## 최소 권한

`quarterly-sre-audit-recovery-lab` Role에서 `pods/exec create`를 제거함. 대신 `sre-pod-recovery-trigger` 단일 ConfigMap에만 `get`, `patch`를 부여함. Pod 조회·감시, 이벤트 조회, 해당 Deployment의 조회·scale patch 권한은 현재 복구 검증에 필요한 범위로 유지함.

## 성공 기준

- 매니페스트에 `pods/exec` 권한이 없음.
- ConfigMap은 지정된 이름·네임스페이스·기본 신호값으로 존재함.
- Runner는 exec 없이 단발성 재시작과 Ready 복귀를 검증함.
- 실패·정상 종료 모두 신호를 `false`로 복구하고 Deployment replicas를 0으로 확인함.
- 기존 분기 감사 계약 테스트 및 보안 스캐너가 통과함.

## 위험과 완화

ConfigMap projected volume 갱신 지연으로 복구 검증이 제한 시간 안에 끝나지 않을 수 있음. Runner는 기존의 제한된 timeout 내에서 실패 처리하며, 정리 경로에서 trigger를 `false`로 복구하고 테스트 Deployment를 0으로 축소함. 운영 서비스와 분리된 테스트 네임스페이스 외에는 쓰기 권한을 추가하지 않음.
