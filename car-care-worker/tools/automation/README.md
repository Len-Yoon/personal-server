# 차량 DB 자동 백업 운영

| 구분 | 내용 | 비고 |
|---|---|---|
| 적용 범위 | 차량 DB 자동 백업·보관 정리·누락 감지와 관리자 상태 표시가 운영 반영됨 | 새 변경은 저장소 운영 승인 기준을 따름 |
| 실행 방식 | user systemd → 차량 전용 host controller → 일자별 단일 K3s Pod | 기존 bootstrap·네 PVC 백업의 시간·실행기 유지, 순차 service 완료 후 차량 연결 |
| 일정 | 매일 02:30 KST 시작, Portal → Book → YouTube → Crawler → 차량 DB | 별도05:30 차량 timer는 disabled/inactive로 유지함 |
| 겹침 회피 | 기존 `pvc-backup-sequence.py`의 `local_lock()`을 그대로 재사용함. 최대 3시간 대기 후 차단함 | 잠금 획득 뒤에도 다른 active backup Job/Pod가 있으면 새 차량 Pod를 만들지 않음 |
| 변경 감지 | 일반 실행은 `--go --if-changed`, 일요일 또는 실제 복원 검증 후 7일 이상 경과 시 `--go` 강제 실행함 | 변경 없어도 주간 실제 암호화·업로드·동일 객체 재다운로드·복원 SHA 검증 수행 |
| 원본 | `/mnt/c/personal-server/data/car-care`를 readOnly hostPath로 mount함 | 차량 DB만 대상, OAuth·차량 자격 증명 데이터 제외 |
| 자격 증명 | 기존 `personal-server/portal-pvc-backup-runtime` Secret을 Pod에 직접 projection함 | 평문 config·환경 파일·호스트 Secret 복제 없음 |
| 공개 소스 | canonical Git blob의 LF·현재 작업 파일 내용을 대조한 뒤 사용자 전용 디렉터리에 stage하여 readOnly mount함 | 차량 backup/policy/Pod runner·공용 복원 helper 네 파일만 포함 |
| 실행 권한 | uid/gid1000, privilege escalation 금지, capability 전체 제거, readOnly root filesystem | ServiceAccount token mount 비활성 |
| scratch | Memory emptyDir 아래 실행 사용자 소유 0700 하위 디렉터리 | inherited setgid를 제거함. age/rclone 작업물은 disposable memory에서만 생성함 |
| 영속 증적 | `~/.local/state/personal-server/car-care-backup/evidence`만 Pod에 writable mount함 | source·controller journal 경로는 Pod에 writable 노출하지 않음 |
| 재시작 | `Restart=no`, Pod `restartPolicy: Never`, 일자별 고정 이름·영속 create intent 사용함 | 생성 응답이 유실되면 조회만 수행하며 같은 작업을 다시 만들지 않음 |
| 중단 | SIGTERM/SIGINT는 Pod terminal까지 처리 지연함. 종료 대기 1800초로 공유잠금을 유지함 | 강제 kill은 피하고 원격 실제 상태·Pod·증적을 먼저 대조해야 함 |
| 불명확 결과 | 이전 일자의 미완료 intent 또는 upload/delete uncertain이 있으면 후속 실행을 차단함 | 자동 재시도·증적 삭제·Pod 재생성 없음 |
| 실패 알림 | 기존 Alertmanager Kubernetes service proxy → 기존 SRE relay 경로를 사용함 | `CarCareBackupFailed`, `service=car-care-worker`, `severity=critical`, 고정 정제 문구만 전송 |
| 알림 실패 | backup 상태와 별도로 `alert_status` 및 `notification-날짜.json`에 기록함. 전체 exit code는 실패로 처리함 | 성공 백업을 실패 백업으로 바꾸거나 원격 업로드를 반복하지 않음 |
| 보관 삭제 | 실제 신규 업로드·복원 성공 후 일간7·주간4·월간3 정책을 자동 적용함 | 최신7개/동률 보존·정확 planSHA·inventory·digest·최신 실복원 확인 후 car 폴더만 삭제 |

## 설치 대상과 실행 인터페이스

- 차량 service는 `car-care-backup.service.tmpl`을 `personal-server-car-care-backup.service`로 등록함. `pvc-backup-sequence-car-care.conf.tmpl`은 기존 user unit의 `personal-server-pvc-backup-sequence.service.d/50-car-care.conf`에 등록함. 기존 네 단계가 성공해 공유 잠금을 해제한 뒤 `after_sequence.py`가 같은 KST 날짜·completed·네 단계 모두passed·완료 시각을 검증한 뒤 blocking `ExecStartPost`로 차량 service를 실행하며, 차량 결과까지 순차 service의 결과에 반영함. 앞 단계 실패 시 차량을 호출하지 않음.
- 이전 `car-care-backup.timer.tmpl`은05:30 분리 일정의 역사적 참고이며 현재 설치·활성화하지 않음. 이미 등록된 차량 timer는 disable --now로 해제하고 파일은 보존함.02:30 기존 timer만 활성 유지함.
- `@REPO_ROOT@`, `@BACKUP_IMAGE@`, `@BACKUP_IMAGE_ID@`를 실제 검증한 값으로 치환함. image는 기존 Portal 백업 도구 이미지의 OCI index digest, image ID는 실제 플랫폼 child manifest digest로 각각 확인함.
- host controller 명령은 `python3 car-care-worker/tools/automation/schedule.py --go --image <고정 이미지 digest> --image-id <플랫폼 manifest digest>`임. Secret 값이나 외부 자격 증명 인자는 받지 않음.
- `.disabled` 파일은 이전 직접 실행 방식의 비활성 참고 템플릿이며 설치하지 않음. 기존 네 서비스 timer·CronJob·실행기는 수정하지 않고 service drop-in으로 마지막 차량 호출만 연결함.

## 사전 확인과 운영 증거

1. CI를 통과한 정확한 커밋을 N100에 fast-forward 동기화하고 canonical source·줄바꿈·image index/child manifest를 확인함. 이미지 pull policy는 `Never`로 유지함. 2. 차량 원본과 evidence의 uid1000 소유권·0700 경로, 기존 네 백업의 완료·복원 증거·공유잠금 해제·active backup 없음, 기존 Secret 존재와 Alertmanager service proxy 접근을 읽기 전용으로 확인함. 3. 최초 승인된 수동 스케줄 실행에서 실제 Pod 결과·고유 backup ID·암호화·동일 객체 복원 SHA·원본 차량 worker 보존·보호 리소스 보존을 확인한 뒤 기존02:30 service에 후속 호출을 연결함. 차량05:30 timer 해제·02:30 다음 실행 시각·user linger와 기존 서비스 비실행 상태를 확인함. 4. 시간은 UTC 인식 ISO 8601로 저장하며 일정·날짜 판단은 KST를 사용함. Pod의 현대 백업 날짜 KST는 UTC+09 고정값으로 계산하여 최소 이미지의 tzdata 설치에 의존하지 않음. 5.30일 지난 성공 Pod·완료scheduled journal 쌍은 UID·명세·날짜·참조증적을 검증한 후5분 검사마다 최대1개만 정리함. 실제 삭제 직전에 fsync intent를 기록하고 응답 불명확 시 새 DELETE 없이 조회로만 확인함. 최신·실패·불확실 기록과 모든 backup/retention evidence·삭제 tombstone은 보존함. 6. `uncertain` 증적과 실패 Pod는 원격 실제 객체·실행 이력을 대조하기 전 수정·삭제하지 않음. 동일 날짜 재진입은 이미 완료된 결과를 재사용하며 새 PASS 시각이나 새 업로드를 만들지 않음.

## 수동 백업과 보관 계획

기존 `backup.py --check`는 읽기 전용·원격 미접속, `--go`는 매번 한 번 업로드·복원 검증, `--go --if-changed`는 논리 내용이 같으면 `SKIP_UNCHANGED`를 기록함. 모든 명령에 `--database`, `--credentials`, `--scratch`, `--evidence`를 명시해야 함. 수동 실행도 동일한 영속 evidence 디렉터리를 사용하고 스케줄러의 공유잠금을 확보해야 함.

보관 기본안은 일간7·이후 주간4·이전 월간3 기간별 최신본에 최신7개 안전 하한과 같은 초의 동률 보존을 추가함. `--retention-plan`은 로컬 증적만 읽으며 실제 삭제는 별도 명시 승인 후 정확한 계획 SHA·remote 확인, 최신24h 증적·최신본 실복원·암호문 digest·원격 inventory 확인을 모두 통과해야 함. 최신 사용자 요청으로 차량 정기 실행에는 위 안전 검증을 통과한 보관 삭제를 포함함. 변경 없음인 날에는 실행하지 않고, 삭제 후보가 없으면 no_op 기록만 남기며 원격 통신하지 않음.

독립 외부 writer가 inventory 검사 직후 동일 이름을 교체하는 경쟁은 로컬 flock으로 제거할 수 없으므로, 이 차량 원격 경로의 단일 writer를 유지해야 함.


## 누락 감지와 관리자 상태

`car-care-backup-monitor.service.tmpl`과 timer를 user unit의 `personal-server-car-care-backup-monitor.service/timer`로 등록함.5분마다 완료journal·실제proof·정리 결과를 읽어 고정 `personal-server/car-care-backup-state`의 `data.status_json`만 CAS로 갱신함. 최초 activation.json에는 실제 활성 UTC시각을 저장함.02:30 이후 활성화한 날은 다음날부터09:00 미완료를 누락으로 통보함. 성공과변경없음·백업완료/정리실패·미완료를 분리하며 백업 자체는 실행하지 않음.

공유 잠금이 점유되면 DEFERRED로 게시·알림·정리를 유예함. OS나 서비스 hang으로 잠금이 계속 유지되면 누락 알림도 지연될 수 있음. 기존 실행 제한 시간을 유지하고 강제 재시작은 추가하지 않음. 실제 복원 증거의 유효 기간은 원본restore_verified_at에서8일이며, observe/skip시각으로 연장하지 않음.

Relay 읽기 권한은 carstate 이름get만 추가하고 Portal은 인증된 관리자 화면에서 Relay의 정제 결과만 읽음. Secret·파일 경로·원격 객체 ID·자격 증명 값은 상태 API에 게시하지 않음. 이번 원격 삭제 대상은 차량 폴더의 검증된 이전 사본뿐이며 다른 서비스 데이터와 백업을 포함하지 않음.

기존02:30 순차 백업과05:30 차량 단독timer 중지 상태는 유지함. 원격2개 암호문 digest·최신본 실제 복원을 읽기 전용 확인했고 두 사본 모두 보존하여 원격 삭제0임. 23:10 정확9653713 Portal·Relay 새 이미지 반입·순차 교체를 완료하고 실제 인증 관리자 차량 표시·비인증 정보 숨김·원본 복원 시각·기존 데이터 및 일정 보존을 확인함. [최신 운영 기록](../../../docs/20260921_프로젝트보완_개발계획.md#2026-10-14-2311-관리자-차량-백업-표시-운영-적용)을 따름.


## 현재 적용·검증 상태

자동 보관 코드·5분 모니터와 Portal·Relay의 인증 관리자 차량 상태 표시가 적용됨. 실제 API·인증 화면·비인증 상세 숨김·원본 복원 시각·기존 데이터와 일정 보존을 확인함. 첫 전체 정기 실행·기간 경과 실제 삭제·Telegram 단말 수신은 추가 관찰이 필요함. [현재 개발·검증 상태](../../../docs/20260921_프로젝트보완_개발계획.md)를 참조함.
