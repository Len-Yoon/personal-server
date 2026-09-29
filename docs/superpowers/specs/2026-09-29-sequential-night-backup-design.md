# 새벽 순차 PVC 백업 설계

## 목적과 범위

네 PVC 백업을 한국 시간 00:30에 한 번 시작하고, Portal → Book Memo → YouTube Memo → News Hub 수집기 순서로 앞 Job의 최종 종료를 확인한 즉시 다음 Job을 시작함. 개별 백업의 암호화·원격 업로드·격리 복원 검증과 각 Secret·PVC·ServiceAccount 경계는 유지함. 한 Job이 최종 실패하면 해당 결과를 기록하고 다음 백업을 시도함. 종료 여부를 확인할 수 없으면 다음 Job을 시작하지 않음.

## 실행 경계

N100 WSL의 systemd 사용자 timer가 기존 `sudo -n k3s kubectl` 권한으로 무자격증명 호스트 조정기를 호출함. 조정기는 저장소에 고정된 네 CronJob의 현재 `jobTemplate`을 읽어 서비스·이미지·명령·PVC·Secret 연결을 검증한 뒤 날짜별 결정적 이름의 Job을 생성함. 기존 네 CronJob은 `suspend: true`로 전환해 독립 예약 중복을 막으며, 기존 시각과 템플릿은 되돌리기 기준으로 보존함. 새 Kubernetes Job 생성 권한을 가진 ServiceAccount는 만들지 않음.

timer는 `OnCalendar=*-*-* 00:30:00 Asia/Seoul`, `Persistent=false`로 설정함. 놓친 실행을 낮에 뒤늦게 시작하지 않으며, 조정기 자체도 06:00 KST 이후 다음 백업 Job을 새로 시작하지 않음. 이미 시작한 Job은 최종 상태까지 관찰하고, 이후 단계는 건너뛰어 알림 상태에 기록함. 각 기존 Job의 최대 실행 시간은 4시간이므로 전체가 반드시 06:00 전에 종료된다는 보장은 없음.

## 중복·장애 처리

날짜와 서비스로 고정된 Job 이름을 사용함. 생성 응답이 불명확하거나 systemd가 재시작하면 해당 이름의 기존 Job을 조회하고 동일한 템플릿인지 확인해 재접속함. Job이 존재하는지 확인할 수 없거나 비정상 템플릿이면 자동 재생성·다음 단계 진행을 중단함. 각 단계의 Complete/Failed 조건을 확인하고, 단순 Pod 부재·API 시간초과는 완료로 간주하지 않음. 호스트 단일 잠금과 systemd 최대 3회 재시도로 중복 실행을 제한함.

고정 ConfigMap `personal-server/pvc-backup-sequence-state`에는 활성 날짜 `active_from`, 명시적 중지 시각 `deactivated_at`, `run_date`, `status`, `current`, `updated_at`, `results`만 저장함. 활성 상태에는 `deactivated_at`을 제거함. `results`는 서비스별 `passed`, `runner_failed`, `failed_before_runner`, `skipped` 중 하나이며 비밀값·Job 로그는 기록하지 않음. 기존 백업 runner가 보고한 실패는 기존 Relay 경로가 알리고, runner 시작 전 실패·건너뜀·조정기 차단·활성 날짜 이후 01:00까지 당일 실행 누락·4시간 15분 이상 진행 상태 미갱신은 Relay가 이 상태를 검증해 한 번씩 알림. Relay는 관찰한 활성 날짜를 별도 상태에 보존해 본 상태 ConfigMap이 사라져도 하루 한 번 상태 누락을 알리며, `deactivated_at`을 확인한 뒤에는 이를 중지함. 이전 상태와 날짜 불일치는 성공으로 취급하지 않음.

## 운영 전환과 복원

맥에서 구현·검증하고 PR CI가 통과한 커밋을 N100에 fast-forward한 뒤, 기존 네 정기 Job과 writer/잠금/복원 증적을 확인함. 고정 상태 ConfigMap과 timer 단위 파일을 준비하되 timer는 비활성으로 둠. Relay 호환 버전을 먼저 적용하고, 네 기존 CronJob을 조건부로 중지하며 시작 마감 300초를 검증한 뒤 timer를 활성화함. Portal 운영 CronJob은 설치용 manifest 전체를 재적용하지 않고 UID·resourceVersion 조건부 patch로 시작 마감과 중지 상태만 변경함. 활성화 직후 예상하지 않은 Job 생성과 네 서비스 외부 health를 확인하고 다음 00:30 실제 실행의 순서·각 복원 결과·알림을 검증함.

복원 시 timer를 먼저 비활성화하고 조정기 및 생성된 Job의 종료를 확인함. 그 뒤 서비스별 운영 목표 파일을 기존 활성 상태로 복원·검증하고 네 CronJob을 기존 시각으로 다시 활성화하며 Relay가 해당 상태를 읽을 수 있는 버전인지 확인함. 제한 적용 도구가 미완료 상태에서 차단하면 실제 Job·잠금·증적을 확인한 뒤 수동 조건부 복구 여부를 판단함. 활성 백업 Job을 삭제하거나 중복 writer를 만들지 않음. N100 타이머·Windows WSL KeepAlive·user linger의 실제 상태와 첫 야간 실행은 운영 적용 시 확인 필요함.
