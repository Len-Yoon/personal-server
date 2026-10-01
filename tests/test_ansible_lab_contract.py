"""Static safety boundaries for the localhost-only Ansible exercise."""

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "infra/ansible-lab"
PROJECT = "personal-server-ansible-lab"


class AnsibleLabContractTests(unittest.TestCase):
    def test_required_lab_files_are_present(self):
        for relative in (
            "ansible.cfg", "inventory/localhost.ini", "group_vars/all.yml",
            "collections/requirements.yml", "playbooks/site.yml",
            "playbooks/rollback.yml", "templates/compose.yaml.j2",
            "templates/index.html.j2",
        ):
            with self.subTest(path=relative):
                self.assertTrue((LAB / relative).is_file(), relative)

    def test_inventory_and_playbooks_cannot_target_remote_or_elevated_hosts(self):
        inventory = (LAB / "inventory/localhost.ini").read_text(encoding="utf-8")
        entries = [line.strip() for line in inventory.splitlines()
                   if line.strip() and not line.lstrip().startswith(("#", "["))]
        self.assertEqual(entries, ["localhost ansible_connection=local"])
        for name in ("site", "rollback"):
            with self.subTest(playbook=name):
                plays = yaml.safe_load((LAB / f"playbooks/{name}.yml").read_text(encoding="utf-8"))
                self.assertEqual(len(plays), 1)
                play = plays[0]
                self.assertEqual(play["hosts"], "localhost")
                self.assertEqual(play["connection"], "local")
                self.assertIs(play["become"], False)
                self.assertNotIn("remote_user", play)

    def test_project_directory_and_dependencies_are_isolated(self):
        vars_ = yaml.safe_load((LAB / "group_vars/all.yml").read_text(encoding="utf-8"))
        self.assertEqual(vars_["lab_project_name"], PROJECT)
        self.assertEqual(vars_["lab_root"], "{{ ansible_env.HOME }}/.local/share/personal-server-ansible-lab")
        self.assertEqual(vars_["lab_http_port"], 18088)
        self.assertEqual(set(vars_), {"lab_project_name", "lab_root", "lab_http_port"})
        requirements = yaml.safe_load((LAB / "collections/requirements.yml").read_text(encoding="utf-8"))
        self.assertEqual([entry["name"] for entry in requirements["collections"]], ["community.docker"])
        self.assertTrue(all(entry.get("version") for entry in requirements["collections"]))

    def test_compose_exposes_only_a_bounded_read_only_sample(self):
        source = (LAB / "templates/compose.yaml.j2").read_text(encoding="utf-8")
        compose = yaml.safe_load(source.replace("{{ lab_http_port }}", "18088"))
        self.assertEqual(compose["name"], PROJECT)
        self.assertEqual(set(compose["services"]), {"sample"})
        service = compose["services"]["sample"]
        self.assertRegex(service["image"], r"^busybox@sha256:[0-9a-f]{64}$")
        self.assertEqual(service["ports"], ["127.0.0.1:18088:8080"])
        self.assertIs(service["read_only"], True)
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertTrue(service["security_opt"] == ["no-new-privileges:true"])
        self.assertGreater(service["cpus"], 0)
        self.assertLessEqual(service["cpus"], 0.25)
        self.assertIn(service["mem_limit"], ("32m", "64m"))
        self.assertGreater(service["pids_limit"], 0)
        self.assertLessEqual(service["pids_limit"], 64)
        self.assertNotIn("privileged", service)
        self.assertNotIn("volumes", service)
        self.assertNotIn("network_mode", service)
        self.assertNotIn("devices", service)
        self.assertNotIn("secrets", compose)
        self.assertEqual(service["configs"], [{"source": "sample_index", "target": "/www/index.html"}])
        self.assertEqual(compose["configs"]["sample_index"]["file"], "./index.html")

    def test_templates_and_playbooks_have_only_sample_content_and_tags(self):
        index = (LAB / "templates/index.html.j2").read_text(encoding="utf-8")
        self.assertIn("ansible lab sample ready", index)
        for name, expected in (("site", {"preflight", "deploy", "verify"}),
                               ("rollback", {"rollback"})):
            plays = yaml.safe_load((LAB / f"playbooks/{name}.yml").read_text(encoding="utf-8"))
            tags = {tag for task in plays[0]["tasks"] for tag in task.get("tags", [])}
            self.assertEqual(tags, expected)
        for relative in (
            "ansible.cfg", "inventory/localhost.ini", "group_vars/all.yml",
            "collections/requirements.yml", "playbooks/site.yml",
            "playbooks/rollback.yml", "templates/compose.yaml.j2",
            "templates/index.html.j2",
        ):
            path = LAB / relative
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotRegex(source, r"(?i)ansible_(?:ssh|password|private_key)|become_user|api[_-]?token")
                self.assertNotRegex(source, r"(?i)(?:infra/compose|infra/k8s|caddy|tunnel|bootstrap|scheduler)")
                self.assertNotRegex(source, r"(?i)\b(?:sudo|ssh|host_network|privileged)\b")


if __name__ == "__main__":
    unittest.main()
