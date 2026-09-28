# 서비스별 PVC 백업 운영 목표 상태 설계

## 목적

맥 저장소에서 Book·YouTube·Crawler PVC 백업 CronJob의 운영 목표 상태를 개발·검증한 뒤, 검증된 Git 커밋을 N100에 동기화해 적용함. N100에서만 수행한 임시 patch를 정상적인 변경 경로로 사용하지 않음.

## 현행과 경계

- 기존 `*-pvc-backup-cronjob.yaml`은 최초 설치용 안전 기본값(`suspend: true`, 실행 불가 sentinel 이미지)임. 전체 manifest를 운영 CronJob에 재적용하지 않음.
- 운영 대상 세 CronJob은 K3s 소유이며 Compose 안전 자동배포 대상이 아님. Secret·PVC·ConfigMap·RBAC·Deployment·이미지 반입은 이 변경의 쓰기 대상에서 제외함.
- Git 병합만으로 N100 설정을 바꾸지 않음. N100 동기화와 적용은 최종 운영 승인 후 수행함.

## 구성

1. `infra/k8s/backup-automation/production-cronjob-state.json`에 세 CronJob의 이름, 일정, 시간대, 시작 마감, 활성 여부, 현재 승인된 이미지 digest를 기록함. 이미지 레지스트리·repository prefix는 도구의 고정 허용 목록으로 검증함.
2. `infra/k8s/tools/service-pvc-backup-production-state.py`는 `--validate`로 목표 파일 형식을 로컬에서 검증하고, `--check`로 N100의 실제 상태와 목표를 읽기 전용 비교함.
3. `--apply --expected-sha <40자 커밋>`은 N100의 깨끗한 추적 작업공간과 `HEAD=origin/main=expected-sha`, 세 백업의 안정 상태를 확인함. 변경이 필요할 때 CronJob의 `startingDeadlineSeconds`와 `suspend`만 UID·resourceVersion·기존 필드 검사 및 server dry-run을 거쳐 JSON Patch함. 이미 일치하면 쓰기 0건임.
4. live 이미지가 목표 digest·고정 이미지 주소와 다르면 중단함. 이미지 변경은 기존 immutable digest 이미지 반입·교체 절차로만 진행하고 목표 파일을 별도 검증함.

## 안전 조건과 결과

- 정확히 세 서비스만 허용함. 이름 중복·누락, 미허용 필드, 잘못된 digest·일정·시간대·시작 마감은 거부함.
- 적용 전 CronJob·Job active 0, writer 1/1, PVC Bound, 백업 상태 `completed`·잠금 해제·증적 존재, Relay 1/1을 확인함. 예정 시각 직후 300초 안에 중지 해제하지 않음.
- 목표 상태 필드와 명령·ServiceAccount·PVC/Secret mount·보안 설정 등 핵심 실행 경계의 드리프트, 이미지·소유권 드리프트 또는 동시 변경은 수정하지 않고 중단함. 이 도구는 CronJob 전체 spec의 포괄적 감사를 대체하지 않음. 여러 대상 중 앞선 적용 후 실패하면 남은 대상은 적용하지 않고 실제 상태를 보고함. 자동 rollback은 하지 않음.
- 적용 후 세 CronJob·Job·writer·PVC·상태·Relay와 외부 health를 확인함. 다음 정기 백업 성공은 별도 증적으로 검증함.

## 지속 규칙

`AGENTS.md`와 운영 문서에 맥 저장소 변경·테스트·PR·CI → 최종 승인 → N100 fast-forward·정확한 커밋 적용·운영 검증 순서를 명시함. 응급 운영 patch가 필요하면 실제 상태를 읽어 기준을 복원하고, 같은 변경을 맥 저장소의 운영 목표 파일에 반영·검증한 뒤 완료 처리함.
