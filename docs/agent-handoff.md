# Agent Handoff

??梨꾪똿?먯꽌 ???꾨줈?앺듃瑜?鍮좊Ⅴ寃??뚯븙?섍린 ?꾪븳 ?붿빟?낅땲??

## Project

`personal-server`??Docker Compose濡?臾띠? 媛쒖씤 ?쒕쾭 ?ы듃?대━???꾨줈?앺듃?낅땲??

- `portal-web`: 硫붿씤 ?ы꽭, ?뚯씪?? 蹂댁븞 ?곹깭 紐⑤떖
- `system-agent`: 誘몃땲 PC/Windows host metrics, 諛깆뾽/?뚯씪??而⑦뀒?대꼫 ?곹깭 API
- `crawler-worker`: Google News RSS ?섏쭛, ????댁뒪, OpenAI ?붿빟
- `youtube-memo`: YouTube 留곹겕蹂?硫붾え
- `book-memo`: 梨?寃?? ?낆꽌 吏꾪뻾瑜? 紐⑹감/硫붾え 愿由?

## Important Files

- `README.md`: ?ы듃?대━?ㅼ슜 ?ㅻ챸怨??쒕퉬?ㅻ퀎 ?ㅽ겕由곗꺑
- `.env.example`: 鍮?媛??덉떆留??좎?
- `docs/cloudflare-tunnel.md`: Cloudflare Tunnel ?댁쁺 ?ㅽ겕由곗꺑
- `docker-compose.yml`: 濡쒖뺄/?댁쁺 compose
- `docker-compose.n100.yml`: N100/MT4 ?숈떆 ?댁쁺??寃쎈웾 compose override
- `docs/images/`: README ?ㅽ겕由곗꺑
- `docs/n100-mt4-setup.md`: Windows N100 + MT4 + WSL2 ?댁쁺 吏移?- `portal-web/app/services/security.py`: 蹂댁븞 ?ㅻ뜑, ?쇰퀎 蹂댁븞 濡쒓렇, 蹂댁븞 ?곹깭 ?곗씠??- `portal-web/app/services/file_store.py`: ?뚯씪????μ냼, ?낅줈???뺤콉, 寃쎈줈 ?덉쟾 泥섎━
- `portal-web/app/routers/dashboard.py`: ?ы꽭 ??쒕낫?? `/admin/security`
- `portal-web/app/routers/files.py`: ?뚯씪???쇱슦?곗? ?몄쬆/??젣 蹂댄샇
- `scripts/maintenance.py`: SQLite 諛깆뾽, ?뚯씪???좏깮 諛깆뾽, 蹂댁븞 濡쒓렇 ?뺣━
- `scripts/windows-host-metrics.ps1`: Windows N100 host ?곹깭瑜?`data/system/host-metrics.json`?쇰줈 湲곕줉
- `system-agent/app/services/metrics.py`: system-agent metrics ?섏쭛/?뺢퇋??- `portal-web/app/services/system_status.py`: ?ы꽭 dashboard??agent fetch, demo/fallback 泥섎━
- `portal-web/app/services/global_search.py`: ?쒕퉬?ㅻ퀎 寃??API 吏묎퀎
- `tests/test_portal_security.py`: ?뚯씪??蹂댁븞 濡쒓렇 ?듭떖 unittest

## Current Security Work

- `portal-web`??蹂댁븞 ?ㅻ뜑 誘몃뱾?⑥뼱 異붽?.
- ?뚯씪 ?낅줈???쒗븳 異붽?:
  - 理쒕? ?ш린: `FILE_MAX_UPLOAD_MB`
  - 李⑤떒 ?뺤옣?? `FILE_BLOCKED_EXTENSIONS`
  - ?덉슜 ?뺤옣??allowlist: `FILE_ALLOWED_EXTENSIONS`
  - ?뺤옣???녿뒗 ?뚯씪 李⑤떒
  - 媛숈? ?대쫫 ??뼱?곌린 李⑤떒
- 蹂댁븞 ?대깽??濡쒓렇 異붽?:
  - ?뚯씪???몄쬆 ?ㅽ뙣
  - ?낅줈???깃났/李⑤떒
  - ?ㅼ슫濡쒕뱶
  - ??젣 ?깃났/?ㅽ뙣
  - 蹂댁븞 ??쒕낫??議고쉶/?ㅽ뙣
- 濡쒓렇???쇰퀎 ?뚯씪濡??앹꽦:
  - 湲곗? env: `SECURITY_LOG_PATH`
  - ?? `security-events-2026-06-29.txt`
  - ?쒓컙? env: `SECURITY_LOG_TIMEZONE`
- ?ы꽭 `蹂댁븞 ?곹깭` 踰꾪듉 異붽?.
- 蹂댁븞 ?곹깭 紐⑤떖? 湲곗〈 愿由ъ옄 ?⑥뒪?뚮뱶 ?꾩슂:
  - ?곗꽑 `FILE_MANAGER_PASSWORD`
  - ?놁쑝硫?`DELETE_PASSWORD`
- ?ы꽭 ?쒕퉬??留곹겕??env 湲곕컲:
  - `NEWS_SERVICE_URL`
  - `YOUTUBE_MEMO_URL`
  - `BOOK_MEMO_URL`
- ?댁쁺 紐⑤뱶 ?뚯씪??蹂댄샇:
  - `APP_ENV=production` ?먮뒗 `FILE_MANAGER_AUTH_REQUIRED=true`?대㈃ `FILE_MANAGER_PASSWORD` ?놁쓣 ??`/files` ?묎렐 李⑤떒

## Maintenance Work

- `scripts/maintenance.py backup`
  - `data/*/*.sqlite3`瑜?`BACKUP_PATH` ?꾨옒 ??꾩뒪?ы봽 ?대뜑濡?諛깆뾽
  - `BACKUP_INCLUDE_FILES=true`????`data/files/`瑜?zip 諛깆뾽
- `scripts/maintenance.py prune-logs`
  - `SECURITY_LOG_RETENTION_DAYS`蹂대떎 ?ㅻ옒???쇰퀎 蹂댁븞 濡쒓렇 ??젣
- `scripts/maintenance.py all`
  - 諛깆뾽怨?濡쒓렇 ?뺣━瑜??④퍡 ?ㅽ뻾

## Mini PC Dashboard Work

- `system-agent` ?쒕퉬??異붽?:
  - `/health`
  - `/metrics`
  - `/metrics/demo`
- ?ы꽭 泥??붾㈃??誘몃땲 PC ?곹깭 ?뱀뀡 異붽?:
  - CPU, 硫붾え由? ?붿뒪?? ?뚯씪??媛쒖닔, 理쒓렐 諛깆뾽
  - 而⑦뀒?대꼫 ?곹깭 紐⑸줉
  - `host_metrics_missing`, `host_metrics_stale`, `backup_missing`, `system_agent_unavailable` 媛숈? 寃쎄퀬
- Windows ?꾩껜 host ?곹깭??Docker 而⑦뀒?대꼫媛 吏곸젒 蹂댁? ?딄퀬 PowerShell collector媛 JSON?쇰줈 ?섍?.
- `DEMO_MODE=true`?대㈃ ?섑뵆 ?쒕쾭 ?곹깭? ?섑뵆 寃??寃곌낵瑜??쒖떆.
- ?ы꽭 ?꾩껜 寃??異붽?:
  - `crawler-worker /api/search`
  - `youtube-memo /api/search`
  - `book-memo /api/search`

## README / Screenshots

README???쒕퉬?ㅻ퀎 ?ㅽ겕由곗꺑怨??ㅻ챸??異붽??덉뒿?덈떎.

- `docs/images/portal-dashboard.png`
- `docs/images/file-manager.png`
- `docs/images/news-hub.png`
- `docs/images/youtube-memo.png`
- `docs/images/book-memo.png`

?뚯씪???ㅽ겕由곗꺑? `?덉떆 ?대뜑1`??蹂댁씠???곹깭濡??ㅼ떆 罹≪쿂?덉뒿?덈떎.

## Env / Git Safety

- ?ㅼ젣 `.env`??而ㅻ컠 湲덉?.
- `.env.example`? 鍮?媛??덉떆留???
- `data/`, SQLite DB, 濡쒓렇, 외부 공개 설정 데이터는 커밋 금지.
- `.gitignore`媛 `.env`, `data/`, `*.sqlite3`, `*.db`, 濡쒓렇, IDE ?뚯씪??臾댁떆??

## Open Items

異붿쿇 ?곗꽑?쒖쐞:

1. Windows ?묒뾽 ?ㅼ?以꾨윭??`scripts/windows-host-metrics.ps1` ?곌껐.
2. 諛깆뾽/濡쒓렇 ?뺣━瑜?N100 cron ?먮뒗 Windows ?묒뾽 ?ㅼ?以꾨윭???곌껐.
3. GitHub 怨듦컻 ??secret scan.
4. Telegram ?깆쑝濡?諛깆뾽 ?ㅽ뙣/?붿뒪??遺議??뚮┝ 異붽?.
5. Docker socket 湲곕컲 ?ㅼ젣 而⑦뀒?대꼫 ?곹깭 ?섏쭛? ?꾩슂???뚮쭔 ?좎쨷??異붽?.

## Verification Already Done

- `python3 -m compileall portal-web/app` ?듦낵.
- `PYTHONPATH=system-agent python3 -m unittest tests.system_agent.test_metrics`濡?system-agent metrics ?뚯뒪??媛??
- Docker ?쒕퉬?ㅺ? ???덈뒗 ?곹깭?먯꽌 Playwright + 濡쒖뺄 Chrome濡?README ?ㅽ겕由곗꺑 罹≪쿂 ?꾨즺.
- ?쇰퀎 濡쒓렇 ?뚯씪紐??앹꽦 ?뺤씤.
- ?뚯씪???낅줈??李⑤떒 ?뺤옣??寃???뺤씤.
- `PYTHONPATH=portal-web python3 -m unittest discover -s tests`濡??듭떖 ?뚯뒪???ㅽ뻾 媛??
