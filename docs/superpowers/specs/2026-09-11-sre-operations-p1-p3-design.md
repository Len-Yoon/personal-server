# SRE 운영 고도화 P1~P3 설계

## 1. 목적

개인 서버의 월간 안전 복구 훈련을 재현 가능한 증적으로 남기고, 공개 서비스 상태 감시 범위를 핵심 기능까지 확장하며, 관리자 상태 화면에서 최근 N100 자동복구 이력을 확인할 수 있게 함.

## 2. 범위와 제외 범위

| 구분 | 포함 | 제외 |
|---|---|---|
| 복구 훈련 | 기존 안전 도구 3개 순차 실행, 결과 JSON 증적 | 자동 scheduler, 실제 Tunnel 중지, Portal 축소·복원, PVC 변경 |
| 공개 감시 | Portal·News·YouTube Memo·Book Memo의 공개 `/health` 집계 | 차량 OAuth, 개인 데이터 화면, 인증 필요 metrics, endpoint별 별도 알림 |
| 관리자 이력 | 정제된 최근 자동복구 이벤트 10건 읽기 전용 표시 | Portal의 Windows 파일 직접 접근, 원본 로그·파일 경로·명령·예외 원문 표시 |

Portal PVC·Secret·운영 데이터·Caddyfile·Tunnel ingress·Compose Portal writer는 변경하지 않음. 새 자격증명·환경 변수·Telegram token은 만들거나 기록하지 않음.

## 3. 월간 안전 복구 훈련

`infra/k8s/tools/monthly-recovery-drill.sh`는 운영자가 월 1회 수동 실행함. 다음 순서만 실행함.

1. `portal-pvc-backup-verify.sh --check`
2. `sre-telegram-verify.sh`
3. `sre-pod-recovery-lab.sh --run`

각 단계 성공 여부, UTC ISO 8601 실행 시각, run ID, 실패 단계만 `~/.local/state/personal-server/recovery-drills/<run-id>.json`에 원자 기록함. 실패 시 이후 단계를 실행하지 않으며, Pod 실습 run ID가 확인되면 cleanup 후 namespace 부재를 확인함. 비밀값·명령 출력·운영 데이터 내용은 저장하지 않음.

## 4. 공개 서비스 상태 감시

GitHub Actions는 아래 고정된 공개 URL을 각각 최대 20초로 점검함.

| 식별자 | URL |
|---|---|
| portal | `https://len.pe.kr/health` |
| news | `https://news.len.pe.kr/health` |
| youtube_memo | `https://memo.len.pe.kr/health` |
| book_memo | `https://books.len.pe.kr/health` |

하나라도 실패하면 단일 공개 서비스 장애로 판정하고, 네 개가 모두 성공해야 복구로 판정함. 기존 단일 GitHub Issue·Telegram 장애/복구 전환 1회 모델을 유지함. Issue에는 실패한 고정 식별자만 기록하고 URL query, 응답 본문, 자격증명은 기록하지 않음.

## 5. 관리자 자동복구 이력

N100 `data/recovery-events.jsonl`은 system-agent만 읽음. system-agent는 허용 필드 `timestamp`, `component`, `event`, `status`, `action`만 가진 최근 10개 객체를 내부 bridge endpoint로 반환함. 줄이 손상되었거나 파일이 없거나 읽기 실패면 빈 목록을 반환하며 오류 원문을 노출하지 않음.

Portal은 기존 system-agent bridge를 통해 이 목록만 요청하고, 비밀번호 인증이 완료된 `/admin/status`에 표로 표시함. Portal에서 KST `YYYY-MM-DD HH:MM`으로 변환함. `accepted`는 조치 실행 수락이고 실제 복구 완료는 `health_restored`임을 화면에 명시함.

## 6. 성공 기준

1. 훈련 실행기는 실제 운영 서비스 변경 없이 성공·실패 증적을 남기고 실패 뒤의 단계를 중단함.
2. 공개 감시는 네 URL 모두 성공할 때만 정상 전환하며 기존 알림 중복 억제를 유지함.
3. 관리자 화면은 인증 후 정제된 자동복구 이력만 표시하며 원본 로그·비밀값·경로·오류 원문을 표시하지 않음.
4. 관련 단위·계약·문서 테스트와 변경 범위 검증을 통과함.
5. 적용 전후 `https://len.pe.kr/health`를 10초 간격으로 3회 호출해 모두 HTTP 200을 확인함.
