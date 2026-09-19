from pathlib import Path
import unittest


ROOT = Path(".")


class DocumentationIndexTests(unittest.TestCase):
    def test_project_readme_exposes_quick_start_verification_and_next_steps(self):
        content = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("## 빠른 상태 확인", content)
        self.assertIn("## 검증", content)
        self.assertIn("## 운영 문서", content)
        self.assertIn("Cloudflare Tunnel", content)
        self.assertIn("K3s", content)
        self.assertIn("Telegram", content)
        self.assertIn("N100 제한형 자동복구", content)

    def test_document_index_exposes_only_current_operations_documents(self):
        content = Path("docs/README.md").read_text(encoding="utf-8")

        self.assertIn("## 현재 운영 기준", content)
        self.assertIn("Cloudflare Tunnel", content)
        self.assertIn("K3s·모니터링·백업", content)
        self.assertIn("codex-work-loop.md", content)
        self.assertIn("agent-loop-evidence.md", content)
        self.assertNotIn("superpowers/", content)

    def test_document_index_exposes_current_roadmap_and_recovery_drill(self):
        content = Path("docs/README.md").read_text(encoding="utf-8")

        self.assertIn("operations-roadmap.md", content)
        self.assertIn("recovery-drill.md", content)

    def test_document_index_exposes_book_memo_k3s_current_runtime_and_historical_cutover_contract(self):
        index = Path("docs/README.md").read_text(encoding="utf-8")
        operations = Path("docs/operations-reference.md").read_text(encoding="utf-8")

        self.assertIn("Book Memo K3s 현재 운영 기준", index)
        self.assertIn("operations-reference.md#book-memo-k3s-현재-운영-기준", index)
        self.assertIn("## Book Memo K3s 현재 운영 기준", operations)
        self.assertIn("## Book Memo K3s 전환 승인·검증", operations)
        for mode in ("`--check`", "`--prepare`", "`--go`", "`--rollback`"):
            self.assertIn(mode, operations)

    def test_operations_roadmap_separates_completed_and_next_work(self):
        content = Path("docs/operations-roadmap.md").read_text(encoding="utf-8")

        self.assertIn("## 현재 완료 기준", content)
        self.assertIn("## 향후 개선 항목", content)
        self.assertIn("GitHub Actions", content)
        self.assertIn("월간 SRE 통합 점검", content)
        self.assertIn("긴급·추가 점검", content)
        self.assertNotIn("월 1회 [복구 훈련 절차]", content)
        self.assertNotIn("UptimeRobot", content)

    def test_operations_roadmap_records_completed_read_only_tunnel_drift_check(self):
        content = Path("docs/operations-roadmap.md").read_text(encoding="utf-8")

        self.assertIn("Cloudflare Tunnel 구성 대조", content)
        self.assertNotIn("| P3 | Cloudflare Tunnel 읽기 전용 설정·문서 drift 점검", content)

    def test_recovery_drill_uses_isolated_safe_tools_and_stop_criteria(self):
        content = Path("docs/recovery-drill.md").read_text(encoding="utf-8")

        self.assertIn("monthly-recovery-drill.sh", content)
        self.assertIn("recovery-drills/<run-id>.json", content)
        self.assertIn("원자적으로 증적화", content)
        self.assertIn("check-portal-backup-evidence.sh", content)
        self.assertIn("monthly-sre-audit", content)
        self.assertIn("5분 외부 상태 감시", content)
        self.assertNotIn("portal-pvc-backup-verify.sh --check", content)
        self.assertIn("sre-telegram-verify.sh", content)
        self.assertIn("sre-pod-recovery-lab.sh --run", content)
        self.assertIn("sre-pod-recovery-lab.sh --cleanup", content)
        self.assertIn("중단", content)
        self.assertIn("운영 데이터", content)
        self.assertIn("3단계 격리된 Pod 자동복구 실습에만 적용", content)

    def test_monthly_sre_docs_record_the_verified_n100_activation_and_review_cycle(self):
        k3s = Path("infra/k8s/README.md").read_text(encoding="utf-8")
        roadmap = Path("docs/operations-roadmap.md").read_text(encoding="utf-8")
        drill = Path("docs/recovery-drill.md").read_text(encoding="utf-8")
        evidence = Path("docs/agent-loop-evidence.md").read_text(encoding="utf-8")

        self.assertIn("N100에서 활성화됨", k3s)
        self.assertNotIn("현재 N100에서 활성화되지 않음", k3s)
        self.assertIn("첫 수동 Job", roadmap)
        self.assertNotIn("월간 복구 훈련 운영 검증", roadmap)
        self.assertIn("정기 운영 결과", drill)
        self.assertIn("월간 SRE 통합 점검 N100 적용", evidence)
        self.assertIn("운영 문서 대조", evidence)

    def test_operations_reference_describes_current_runtime_split(self):
        content = Path("docs/operations-reference.md").read_text(encoding="utf-8")

        self.assertIn("Cloudflare Tunnel → Caddy → K3s Portal", content)
        self.assertIn("portal-web", content)
        self.assertIn("crawler-worker", content)
        self.assertIn("공개 상태 Telegram 알림", content)
        self.assertIn("personal-server-autostart", content)
        self.assertIn("HOMEOPS_EXECUTOR_SHARED_SECRET", content)
        self.assertIn("fail-closed", content)
        self.assertIn("safe_cd_skip_k3s_service", content)

    def test_safe_deploy_docs_distinguish_k3s_memo_classification_from_compose_deployment(self):
        content = Path("docs/n100-github-auto-deploy.md").read_text(encoding="utf-8")

        self.assertIn("`youtube-memo`", content)
        self.assertIn("`book-memo`", content)
        self.assertIn("safe_cd_skip_k3s_service", content)

    def test_public_route_docs_distinguish_the_car_callback_route_from_caddy(self):
        tunnel = Path("docs/cloudflare-tunnel.md").read_text(encoding="utf-8")
        operations = Path("docs/operations-reference.md").read_text(encoding="utf-8")

        self.assertIn("`Caddyfile`에는 `car.len.pe.kr` 호스트 블록이 없음", tunnel)
        self.assertIn("Cloudflare Tunnel의 별도 ingress", tunnel)
        self.assertIn("별도 Cloudflare Tunnel ingress", operations)

    def test_tunnel_documentation_matches_all_k3s_application_caddy_paths_without_private_address(self):
        tunnel = Path("docs/cloudflare-tunnel.md").read_text(encoding="utf-8")

        self.assertIn("Cloudflare Tunnel → WSL loopback Caddy → K3s Portal·Crawler Worker·Book Memo·YouTube Memo", tunnel)
        self.assertIn("`news.len.pe.kr` | WSL loopback Caddy | K3s `crawler-worker` Service", tunnel)
        self.assertIn("`books.len.pe.kr` | WSL loopback Caddy | K3s `book-memo` Service", tunnel)
        self.assertIn("`memo.len.pe.kr` | WSL loopback Caddy | K3s `youtube-memo` Service", tunnel)
        self.assertIn("뉴스는 Caddy를 거쳐 K3s `crawler-worker` Service", tunnel)
        self.assertNotIn("Compose `crawler-worker` loopback endpoint", tunnel)
        self.assertNotIn("뉴스는 Caddy를 거치지 않고", tunnel)
        self.assertNotIn("`books.len.pe.kr` | Compose `book-memo` loopback endpoint", tunnel)
        self.assertNotIn("`memo.len.pe.kr` | Compose `youtube-memo` loopback endpoint", tunnel)
        self.assertIn("비공개 callback upstream", tunnel)
        self.assertNotIn("http://localhost:8015", tunnel)

    def test_operations_reference_records_book_memo_k3s_cutover_boundary(self):
        operations = Path("docs/operations-reference.md").read_text(encoding="utf-8")
        index = Path("docs/README.md").read_text(encoding="utf-8")

        self.assertIn("Cloudflare Tunnel → Caddy → K3s Portal·Crawler Worker·Book Memo·YouTube Memo", operations)
        self.assertIn("`book-memo` | K3s `personal-server` namespace", operations)
        self.assertIn("root 소유 runtime state marker", operations)
        self.assertIn("정적 ClusterIP는 Git에 기록하지 않음", operations)
        self.assertIn("`data/book-memo`", operations)
        self.assertIn("정적 `book-memo.yaml`을 재적용하지 않음", operations)
        self.assertIn("K3s 단일 writer", operations)
        self.assertNotIn("뉴스·YouTube 메모·책 메모는 Tunnel 직접 ingress", operations)
        self.assertIn("비공개 callback upstream", operations)
        self.assertIn("Book Memo K3s 현재 운영 기준", index)

    def test_crawler_k3s_current_runtime_docs_preserve_the_single_writer_boundary(self):
        project_readme = Path("README.md").read_text(encoding="utf-8")
        index = Path("docs/README.md").read_text(encoding="utf-8")
        operations = Path("docs/operations-reference.md").read_text(encoding="utf-8")
        tunnel = Path("docs/cloudflare-tunnel.md").read_text(encoding="utf-8")
        roadmap = Path("docs/operations-roadmap.md").read_text(encoding="utf-8")

        self.assertIn("Crawler Worker K3s 현재 운영 기준", project_readme)
        self.assertIn("K3s `crawler-worker`", project_readme)
        self.assertIn("뉴스 수집 K3s 현재 운영 기준", index)
        self.assertIn("operations-reference.md#뉴스-수집-k3s-현재-운영-기준", index)
        self.assertIn("## 뉴스 수집 K3s 현재 운영 기준", operations)
        self.assertIn("`crawler-worker=k3s`", operations)
        self.assertIn("`news_archive.json`", operations)
        self.assertIn("`news_collection_status.json`", operations)
        self.assertIn("롤백 자산", operations)
        self.assertIn("native `crawler-worker` Service", operations)
        self.assertIn("root 소유 runtime state marker", operations)
        self.assertIn("외부 health 3회", operations)
        self.assertIn("Docker `crawler-worker`를 다시 기동하지 않음", operations)
        self.assertIn("Cloudflare Tunnel → Caddy → K3s `crawler-worker` Service", operations)
        self.assertIn("뉴스는 Caddy를 거쳐 K3s `crawler-worker` Service", tunnel)
        self.assertIn("Crawler Worker K3s 이전", roadmap)
        self.assertNotIn("Crawler Worker K3s 전환 준비", roadmap)

    def test_architecture_diagram_records_current_routes_and_operational_checks(self):
        content = Path("docs/images/personal-server-architecture-v2.svg").read_text(
            encoding="utf-8"
        )

        self.assertIn("Cloudflare Tunnel → Caddy → K3s Service", content)
        self.assertIn("Portal · News · YouTube · Books", content)
        self.assertIn("portal-web Service", content)
        self.assertIn("crawler-worker Service", content)
        self.assertIn("youtube-memo Service", content)
        self.assertIn("book-memo Service", content)
        self.assertIn("서비스별 PVC · 단일 writer", content)
        self.assertIn("Portal PVC · Crawler PVC · YouTube PVC · Book PVC", content)
        self.assertIn("비공개 callback upstream", content)
        self.assertIn("GitHub Actions · 약 5분 공개 health", content)
        self.assertIn("NewsCollectionStale → Alertmanager → SRE relay → Telegram", content)
        self.assertIn("NodePort 이상은 Tunnel 복구 제외", content)
        self.assertNotIn("Portal 경로만 전달", content)
        self.assertNotIn("News만 Tunnel 직접 ingress", content)
        self.assertNotIn("Compose 직접 ingress", content)
        self.assertNotIn("GitHub Actions · 일일 점검", content)
        self.assertIn("월간 SRE 통합 점검", content)

    def test_n100_recovery_and_uptime_alert_conditions_are_documented(self):
        n100 = Path("docs/n100-mt4-setup.md").read_text(encoding="utf-8")
        uptime = Path("docs/public-uptime-monitor.md").read_text(encoding="utf-8")

        self.assertIn("3분 간격", n100)
        self.assertIn("2회 연속", n100)
        self.assertIn("최대 3회", n100)
        self.assertIn("Tunnel 장애 알림을 1회 전송하는 데 성공한 경우", n100)
        self.assertIn("복구 알림을 1회", n100)
        self.assertIn("독립 보완 경로", n100)
        self.assertIn("-Supervisor", n100)
        self.assertIn("Daemon", n100)
        self.assertIn("120초", n100)
        self.assertIn("15초", n100)
        self.assertIn("공개 `https://len.pe.kr/health`", n100)
        self.assertIn("NodePort가 비정상이면 Tunnel 상태는 보류", n100)
        self.assertIn("Telegram 장애 메시지 전송이 실패하면", uptime)
        self.assertIn("복구 전환 메시지를 보내지 않음", uptime)
        self.assertIn("Supervisor", uptime)
        self.assertIn("알림 미전송", uptime)

    def test_public_uptime_docs_distinguish_monitor_path_failures_and_news_collection_boundary(self):
        uptime = Path("docs/public-uptime-monitor.md").read_text(encoding="utf-8")
        roadmap = Path("docs/operations-roadmap.md").read_text(encoding="utf-8")

        self.assertIn("감시 실행 실패", uptime)
        self.assertIn("Telegram 전달 실패", uptime)
        self.assertIn("다음 성공적으로 끝난 점검", uptime)
        self.assertIn("예약 실행 자체가 시작되지 않는 경우", uptime)
        self.assertIn("외부 제공자 없이 감지할 수 없음", uptime)
        self.assertIn("자동 재시작 또는 자동 복구를 수행하지 않음", uptime)
        self.assertIn("감시 경로 식별·알림 보강", roadmap)
        self.assertIn("Cloudflare Tunnel 구성 대조", roadmap)
        self.assertIn("YouTube Memo K3s 이전", roadmap)

    def test_public_uptime_docs_explain_service_specific_telegram_messages(self):
        uptime = Path("docs/public-uptime-monitor.md").read_text(encoding="utf-8")

        self.assertIn("[외부 장애]", uptime)
        self.assertIn("[외부 복구]", uptime)
        self.assertIn("News Hub", uptime)
        self.assertIn("서비스별 사용자용 이름", uptime)
        self.assertIn("내부 IP", uptime)

    def test_reboot_docs_describe_post_boot_check_without_new_telegram_message(self):
        uptime = Path("docs/public-uptime-monitor.md").read_text(encoding="utf-8")

        self.assertIn("초기 대기 후 `post_boot_check`를 정확히 1회 수행함", uptime)
        self.assertIn("WSL, K3s, Portal 상태를 확인", uptime)
        self.assertIn("10초 간격으로 3회 호출해 모두 HTTP 200인지 확인", uptime)
        self.assertIn("`recovery-events.jsonl`에 이벤트로만 기록", uptime)
        self.assertIn("초기 점검 자체에 대한 신규 Telegram 메시지는 발송하지 않음", uptime)
        self.assertIn("Cloudflare Tunnel 장애·복구 전환에만 사용", uptime)
        self.assertIn("N100 적용과 검증을 완료함", uptime)
        self.assertIn("2026-09-18 15:49", uptime)
        self.assertIn("외부 health는 10초 간격 3회 모두 HTTP 200으로 확인함", uptime)
        self.assertNotIn("N100에는 아직 적용·검증하지 않음", uptime)

    def test_project_readme_describes_search_status_and_news_collection_freshness(self):
        content = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("전체 검색", content)
        self.assertIn("현재 응답 없음", content)
        self.assertIn("수집 최신성", content)
        self.assertIn("마지막 정상 수집", content)

    def test_reboot_docs_require_boot_trigger_keepalive_and_password_prompt(self):
        mt4 = (ROOT / "docs" / "n100-mt4-setup.md").read_text(encoding="utf-8")

        self.assertIn("BootTrigger", mt4)
        self.assertIn("Password", mt4)
        self.assertIn("로그인 없이", mt4)

    def test_reboot_health_docs_require_exact_200_and_failure_exit(self):
        mt4 = (ROOT / "docs" / "n100-mt4-setup.md").read_text(encoding="utf-8")

        self.assertIn("curl --output /dev/null --silent --show-error --write-out '%{http_code}'", mt4)
        self.assertIn('[ "$curl_exit" -ne 0 ] || [ "$http_code" != "200" ]', mt4)
        self.assertIn('exit "$failed"', mt4)

    def test_n100_docs_document_reboot_limits_and_tunnel_service_recovery(self):
        n100 = Path("docs/n100-mt4-setup.md").read_text(encoding="utf-8")
        tunnel = Path("docs/cloudflare-tunnel.md").read_text(encoding="utf-8")
        drill = Path("docs/recovery-drill.md").read_text(encoding="utf-8")

        self.assertIn("PersonalServer-EmergencyReboot", n100)
        self.assertIn("20분", n100)
        self.assertIn("6시간", n100)
        self.assertIn("shutdown /a", n100)
        self.assertIn("Tunnel·Portal·NodePort 단독 장애", n100)
        self.assertIn("cloudflared-personal-server.service", tunnel)
        self.assertIn("Tunnel만", drill)

    def test_k3s_portal_environment_example_and_observability_prerequisite_are_documented(self):
        k3s = Path("infra/k8s/README.md").read_text(encoding="utf-8")
        operations = Path("docs/operations-reference.md").read_text(encoding="utf-8")

        self.assertIn("PORTAL_UPSTREAM=host.docker.internal:30080", k3s)
        self.assertIn("native `crawler-worker` Service", operations)
        self.assertNotIn("현재 Prometheus 수집은 Portal cutover가 만든", operations)

    def test_k3s_docs_describe_suspended_cronjob_backup_cutover(self):
        k3s = Path("infra/k8s/README.md").read_text(encoding="utf-8")
        drill = Path("docs/recovery-drill.md").read_text(encoding="utf-8")

        self.assertIn("portal-pvc-backup-cronjob.sh --preflight", k3s)
        self.assertIn("portal-pvc-backup-cronjob.sh --activate", k3s)
        self.assertIn("suspend: true", k3s)
        self.assertIn("Secret 값", k3s)
        self.assertIn("CronJob", drill)
        self.assertIn("단일 스케줄러", drill)

    def test_subagent_workflow_defines_mandatory_routes(self):
        project_rules = Path("AGENTS.md").read_text(encoding="utf-8")
        workflow = Path("docs/codex-work-loop.md").read_text(encoding="utf-8")

        self.assertIn("기능 구현·테스트·설정 파일", project_rules)
        self.assertIn("전문 검토 에이전트를 필수로 포함해 최대 4명", project_rules)
        self.assertIn("에이전트 운영 기록", workflow)
        self.assertIn("전문 검토를 필수로 적용함", workflow)

    def test_agent_handoff_documents_the_ci_equivalent_local_test_runner(self):
        handoff = Path("docs/agent-handoff.md").read_text(encoding="utf-8")

        self.assertIn("tests/run_service_tests.py", handoff)
        self.assertIn("CI와 동일한 서비스별 격리", handoff)
        self.assertIn("python3 tests/run_service_tests.py", handoff)


if __name__ == "__main__":
    unittest.main()
