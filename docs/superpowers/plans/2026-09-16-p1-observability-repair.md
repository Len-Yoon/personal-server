# P1 관측성 수집 복구 구현 계획

## Task 1: Prometheus discovery 원인과 계약 테스트

- crawler ServiceMonitor, Service, EndpointSlice와 Prometheus dropped target을 대조함.
- discovery 계약을 보장하는 실패 테스트를 추가함.

## Task 2: 수집기 scalar 집계 보정

- Portal Ready 쿼리가 중복 시계열에서도 scalar를 반환하도록 실패 테스트 후 최소 수정함.
- 기존 `unobservable` fail-closed 동작 회귀를 확인함.

## Task 3: crawler discovery 최소 복구

- Task 1에서 확정한 비밀값 없는 매니페스트 계약만 수정함.
- Prometheus target과 freshness scalar를 실제 N100에서 확인함.

## Task 4: 통합 검증과 운영 적용

- 관련 단위·K8s 계약·maintenance CI를 실행하고 독립 검토함.
- 병합 후 N100에서 이미지 반입, 수동 Job 성공, 증적 구조 검증, CronJob 활성화, 외부 health 검증을 수행함.
