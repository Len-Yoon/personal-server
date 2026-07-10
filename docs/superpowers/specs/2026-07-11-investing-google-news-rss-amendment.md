# Investing.com 수집 방식 전환 기록

## 전환 이유

Playwright 헤드리스 Chromium은 Investing.com 한국어 뉴스 페이지에서 Cloudflare 보안 확인 화면만 받고 뉴스 목록을 받지 못했다. 실제 Chrome 화면 모드에서도 로봇 확인이 반복되어 하루 1회 무인 운영에는 적합하지 않았다.

## 확정 방식

Google News RSS의 `site:kr.investing.com/news` 검색 결과를 수집하고, RSS의 출처가 정확히 `Investing.com 한국어`인 항목만 남긴다. 제목 끝의 `- Investing.com 한국어`와 `By Investing.com` 표시는 제거하고, `By Investing.com`만 있는 비정상 항목은 버린다.

RSS 항목의 Google News 링크, UTC 게시 시각, 한국시간 표시 시각, 출처를 Obsidian 날짜별 Markdown으로 저장한다. 기사 본문과 AI 요약은 저장하지 않는다.

## 운영 특성

- Investing.com 직접 페이지의 모든 기사를 보장하지 않는다.
- Google News 노출 지연 또는 누락이 있을 수 있다.
- 하루 1회 수집과 N100·MT4 병행에는 적합하다.
- Playwright, Chromium, CAPTCHA 처리, 외부 AI 토큰이 필요 없다.
