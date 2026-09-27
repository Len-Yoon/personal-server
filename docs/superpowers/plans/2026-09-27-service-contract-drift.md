# 서비스 공통 계약 drift 축소 계획

## 목적과 범위

Portal·Crawler·Book·YouTube의 URL·시간·인증 계약을 현재 코드와 회귀 테스트로 대조함. 확인된 차이만 수정하며 Docker build context, 운영 배포, A9·maintenance·backup 파일은 변경하지 않음.

| 계약 | 현재 근거 | 이번 처리 |
|---|---|---|
| 포털 복귀 URL | 네 서비스의 `host_urls.py`가 같은 공개 포털 도메인을 계산함. Crawler만 `X-Forwarded-Host`의 포트를 제거함 | 실제 drift인 Portal·Book·YouTube의 forwarded host 포트 정규화를 Crawler와 맞춤. `Host` 헤더·지역/공개 분기와 명시 `PORTAL_HOME_URL` 유지 |
| 시간 표시 | Book·YouTube는 SQLite/ISO 입력, Crawler는 RSS 날짜, Portal은 상태 기록과 ` KST` 입력을 지원함 | 공통 UTC→Asia/Seoul `YYYY-MM-DD HH:MM` 계약과 각 서비스의 고유 입력 형식을 교차 검증. 코드 변경 없음 |
| 인증 | Book·YouTube는 쓰기 세션과 실패 제한을 사용하고 Portal은 권한 scope별 세션을 사용함 | 기존 서비스별 인증 회귀를 실행해 보존 확인. 동작 변경이나 공유 모듈 추출 없음 |

## 절차·검증

1. 교차 서비스 URL·시간 계약 테스트를 먼저 작성하고 forwarded port 사례의 실패를 확인함.
2. 세 `host_urls.py`만 최소 수정하고 관련 서비스 테스트·문서 계약·정적 검사·하네스 수행함.
3. 인증은 Book·YouTube·Portal의 기존 서비스 테스트로 확인하고, 별도 공용 세션 추출을 이번 변경의 성과로 주장하지 않음.

## 경계

서비스별 Dockerfile은 자기 디렉터리만 이미지에 복사함. 저장소 상위 공용 패키지를 직접 import하면 운영 이미지에서 누락되므로 추가하지 않음. Crawler의 RSS·Portal의 상태 기록 등 입력 형식 차이도 유지함. N100 운영 변경은 수행하지 않음.
