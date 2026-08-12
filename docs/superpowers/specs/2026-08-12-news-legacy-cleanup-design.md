# 뉴스 레거시 경로 및 환경변수 정리 설계

## 목적

현재 공개 뉴스 화면과 스케줄러에서 사용하지 않는 글로벌 시장 다중 소스 수집 경로를 제거하고, 서비스 코드에서 읽지 않는 OpenAI 환경변수 예시를 정리함.

## 유지 범위

- `KR_WORLD`: Investing.com 한국어 RSS 수집과 나스닥 관련성·Telegram 알림을 유지함.
- `KR_IT`, `KR_AI`: Google News RSS 기반 화면을 유지함.
- 뉴스 보관·검색·자동 갱신·CSP·Origin 검증을 유지함.
- Caddy는 N100 자동기동과 배포 스크립트가 실제 기동 대상으로 선언하므로 변경하지 않음.

## 삭제 범위

- 공개 라우트에서 호출되지 않는 `WORLD`, `NASDAQ`, `GOLD`, `HK50` 수집 API와 카테고리 정의
- Reuters, AP, MarketWatch 수집기와 해당 전용 테스트
- 글로벌 다중 소스 선택 로직과 해당 테스트
- 현재 추적된 서비스 코드가 읽지 않는 `OPENAI_API_KEY`, `OPENAI_SUMMARY_MODEL` 환경변수 예시와 운영 문서 설명

## 검증 기준

- `collect_market_news`와 글로벌 카테고리 API가 제거되어야 함.
- 한국어 세 카테고리, Investing.com·Google News 수집, 나스닥 Telegram 정책 테스트가 통과해야 함.
- `.env.example`과 운영 문서에 사용하지 않는 OpenAI 환경변수 설명이 없어야 함.
- `git diff --check`가 통과해야 함.
