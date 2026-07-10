# Investing.com 한국어 뉴스의 Obsidian 저장 설계

## 목표

하루 한 번 한국어 Investing.com 뉴스 목록을 브라우저로 수집하고, 뉴스 본문이나 AI 요약 없이 제목·게시 시간·링크·출처만 날짜별 Markdown 파일로 저장한다. 파일은 사용자가 Obsidian Vault로 열어 읽을 수 있어야 하며, 현재 N100 PC의 MT4 자동매매와 기존 서버 서비스에 영향을 최소화한다.

## 범위

포함:

- `https://kr.investing.com/news` 및 주식시장 뉴스 목록 수집
- Playwright Chromium을 이용한 렌더링 후 DOM 파싱
- 제목, URL, 표시된 게시 시간, 출처 추출
- URL 기준 중복 제거
- 날짜별 Markdown 생성 또는 갱신
- 설정 가능한 Vault 경로와 뉴스 수집 개수
- 실패 시 재시도, 기존 파일 보존, 실행 로그

제외:

- Investing.com 기사 본문 수집·재배포
- AI 요약 또는 외부 API 호출
- CAPTCHA·로그인·봇 차단 우회
- Obsidian 앱 또는 Chrome 확장 설치
- 기존 `crawler-worker`의 RSS 뉴스 흐름 변경

## 아키텍처

기존 API 서비스와 분리된 일회성 작업으로 구현한다.

```text
Windows 작업 스케줄러 또는 수동 실행
        |
        v
investing-news-importer 일회성 실행
        |
        v
Playwright Chromium (headless, 단일 페이지)
        |
        v
한국어 뉴스 목록 DOM 파싱
        |
        v
중복 제거 및 Markdown 렌더링
        |
        v
Obsidian Vault/뉴스/Investing/YYYY-MM-DD.md
```

수집기는 기존 `crawler-worker`의 HTTP 요청 경로와 분리한다. 브라우저 의존성으로 기존 서비스 이미지가 불필요하게 커지거나 MT4와 동시에 상주하는 것을 방지하기 위해, 동일 저장소 안에 별도 실행 모듈과 전용 Docker 실행 경로를 둔다.

## 브라우저 수집 동작

Playwright로 Chromium을 한 번 시작하고 뉴스 목록 페이지 하나를 연다. 페이지 로딩 후 뉴스 링크를 찾고, 필요할 때만 제한된 횟수로 스크롤해 목록을 채운다. 이미지·영상·폰트·광고성 리소스는 차단하고 기사 상세 페이지는 열지 않는다.

일반적인 브라우저 동작만 사용하며, 좌표 기반 마우스 자동화나 탐지 회피 코드는 넣지 않는다. 로그인 또는 CAPTCHA 화면이 감지되면 작업을 실패 처리한다.

페이지에 정확한 시간 데이터가 있으면 이를 사용한다. 상대 시간만 있으면 화면에 표시된 문자열을 보존하고 별도로 수집 시각을 기록한다. 출처는 byline에서 추출하며 없을 경우 `Investing.com`으로 보정한다.

## 출력 형식

환경변수:

```env
OBSIDIAN_VAULT_PATH=/vault
OBSIDIAN_NEWS_DIR=뉴스/Investing
INVESTING_NEWS_URL=https://kr.investing.com/news
INVESTING_NEWS_LIMIT=50
INVESTING_NEWS_TIMEZONE=Asia/Seoul
```

출력 경로는 `<OBSIDIAN_VAULT_PATH>/<OBSIDIAN_NEWS_DIR>/<YYYY-MM-DD>.md`이다. 새 파일은 다음 형식을 따른다.

```markdown
# Investing.com 한국어 뉴스 - 2026-07-10

수집 시각: 2026-07-10 06:30:00 KST
수집 대상: https://kr.investing.com/news

## 뉴스 목록

- [뉴스 제목](https://kr.investing.com/...)
  - 게시 표시: 23분 전
  - 출처: Investing.com
```

같은 날짜 파일이 이미 있으면 URL을 기준으로 기존 항목과 새 항목을 합치고, 같은 URL은 한 번만 남긴다. 저장은 임시 파일에 먼저 수행한 뒤 성공 시 교체해 부분 파일을 남기지 않는다.

## 오류 처리와 운영

- 페이지 접속 또는 로딩 실패: 제한 시간 내 1회 재시도
- 뉴스 항목이 0개: 기존 Markdown을 수정하지 않고 실패 로그 기록
- 일부 항목만 추출: 추출된 항목을 저장하고 경고 로그 기록
- Vault 경로 미설정 또는 쓰기 실패: 명확한 오류 메시지와 비정상 종료
- CAPTCHA·로그인 요구: 우회하지 않고 비정상 종료
- 기본 실행은 하루 1회, 거래 시작 전 시간대로 운영

수집기는 완료 후 Chromium과 프로세스를 종료한다. N100용 compose에서는 CPU와 메모리 제한을 별도로 둔다. 실패해도 MT4와 상시 실행 중인 API 서비스는 계속 동작해야 한다.

## 테스트 전략

단위 테스트:

- HTML fixture에서 제목·링크·게시 시간·출처 추출
- 상대 시간과 정확한 시간 처리
- URL 정규화 및 중복 제거
- Markdown 생성 및 기존 파일 병합
- 빈 결과와 오류 상황에서 기존 파일 보존

통합 테스트:

- Playwright Chromium으로 한국어 뉴스 페이지에 접속
- 최소 5개 뉴스 항목을 추출
- 임시 Vault에 날짜별 Markdown 생성
- 두 번 실행해 중복 항목이 늘지 않는지 확인

실제 운영 전 N100에서 MT4 실행 중 수집 작업을 1회 수행하고, MT4 지연 여부와 작업 전후 메모리 사용량을 확인한다.

## 성공 기준

- 하루 1회 실행으로 한국어 뉴스 목록의 메타데이터를 안정적으로 저장한다.
- 생성된 Markdown이 Obsidian에서 바로 열린다.
- 같은 뉴스가 반복 실행으로 중복되지 않는다.
- AI 토큰이나 Obsidian 앱 실행 없이 작업이 완료된다.
- 수집 실패가 MT4 자동매매 또는 기존 서버 API를 중단시키지 않는다.
