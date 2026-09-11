from pathlib import Path
import unittest


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

    def test_operations_roadmap_separates_completed_and_next_work(self):
        content = Path("docs/operations-roadmap.md").read_text(encoding="utf-8")

        self.assertIn("## 현재 완료 기준", content)
        self.assertIn("## 향후 개선 항목", content)
        self.assertIn("GitHub Actions", content)
        self.assertNotIn("UptimeRobot", content)

    def test_recovery_drill_uses_isolated_safe_tools_and_stop_criteria(self):
        content = Path("docs/recovery-drill.md").read_text(encoding="utf-8")

        self.assertIn("monthly-recovery-drill.sh", content)
        self.assertIn("recovery-drills/<run-id>.json", content)
        self.assertIn("원자적으로 증적화", content)
        self.assertIn("portal-pvc-backup-verify.sh --check", content)
        self.assertIn("sre-telegram-verify.sh", content)
        self.assertIn("sre-pod-recovery-lab.sh --run", content)
        self.assertIn("sre-pod-recovery-lab.sh --cleanup", content)
        self.assertIn("중단", content)
        self.assertIn("운영 데이터", content)
        self.assertIn("3단계 격리된 Pod 자동복구 실습에만 적용", content)

    def test_operations_reference_describes_current_runtime_split(self):
        content = Path("docs/operations-reference.md").read_text(encoding="utf-8")

        self.assertIn("Cloudflare Tunnel → Caddy → K3s Portal", content)
        self.assertIn("portal-web", content)
        self.assertIn("crawler-worker", content)
        self.assertIn("공개 상태 Telegram 알림", content)
        self.assertIn("personal-server-autostart", content)
        self.assertIn("HOMEOPS_EXECUTOR_SHARED_SECRET", content)
        self.assertIn("fail-closed", content)

    def test_public_route_docs_distinguish_the_car_callback_route_from_caddy(self):
        tunnel = Path("docs/cloudflare-tunnel.md").read_text(encoding="utf-8")
        operations = Path("docs/operations-reference.md").read_text(encoding="utf-8")

        self.assertIn("`Caddyfile`에는 `car.len.pe.kr` 호스트 블록이 없음", tunnel)
        self.assertIn("Cloudflare Tunnel의 별도 ingress", tunnel)
        self.assertIn("별도 Cloudflare Tunnel ingress", operations)

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
        self.assertIn("portal-compose-bridge", operations)
        self.assertIn("compose-crawler", operations)

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


if __name__ == "__main__":
    unittest.main()
