"""Static safety boundaries for the localhost-only Ansible exercise."""

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "infra/ansible-lab"
PROJECT = "personal-server-ansible-lab"


class AnsibleLabContractTests(unittest.TestCase):
    def _tasks(self, name):
        return yaml.safe_load((LAB / f"playbooks/{name}.yml").read_text(encoding="utf-8"))[0]["tasks"]

    def test_extra_vars_cannot_redirect_lab_targets(self):
        for name in ("site", "rollback"):
            with self.subTest(playbook=name):
                tasks = self._tasks(name)
                first = tasks[0]
                self.assertIn("ansible.builtin.assert", first)
                assertions = " ".join(first["ansible.builtin.assert"]["that"])
                self.assertIn("lab_project_name == 'personal-server-ansible-lab'", assertions)
                self.assertIn("lab_http_port", assertions)
                self.assertIn("18088", assertions)
                self.assertIn("lab_root", assertions)
                self.assertIn("lookup('ansible.builtin.env', 'HOME')", assertions)

    def test_project_collision_is_checked_without_lab_directory(self):
        for name in ("site", "rollback"):
            with self.subTest(playbook=name):
                tasks = self._tasks(name)
                queries = [task for task in tasks if task.get("register", "").startswith("lab_project_")]
                self.assertEqual(len(queries), 3)
                for task in queries:
                    self.assertIn("ansible.builtin.command", task)
                    self.assertNotIn("when", task)
                    self.assertIs(task["changed_when"], False)
                guards = [task for task in tasks if "ansible.builtin.assert" in task]
                self.assertTrue(any("lab_project_containers.stdout" in str(task) and
                                    "lab_directory.stat.exists" in str(task) for task in guards))

    def test_existing_docker_resources_require_compose_ownership_labels(self):
        for name in ("site", "rollback"):
            with self.subTest(playbook=name):
                tasks = self._tasks(name)
                inspections = [task for task in tasks if task.get("register") in
                               {"lab_container_labels", "lab_network_labels"}]
                self.assertEqual(len(inspections), 2)
                self.assertTrue(all("ansible.builtin.command" in task for task in inspections))
                assertions = " ".join(str(task.get("ansible.builtin.assert", {}).get("that", ""))
                                      for task in tasks)
                self.assertIn("com.docker.compose.project.working_dir", assertions)
                self.assertIn("com.docker.compose.project.config_files", assertions)
                self.assertIn("com.docker.compose.service", assertions)
                self.assertIn("com.docker.compose.network", assertions)
                self.assertIn("lab_root", assertions)

    def test_expected_resource_names_are_checked_without_project_label(self):
        for name in ("site", "rollback"):
            with self.subTest(playbook=name):
                tasks = self._tasks(name)
                by_name = {task.get("register"): task for task in tasks
                           if task.get("register") in {"lab_named_container", "lab_named_network"}}
                self.assertEqual(set(by_name), {"lab_named_container", "lab_named_network"})
                for task in by_name.values():
                    self.assertNotIn("when", task)
                    self.assertNotIn("com.docker.compose.project", str(task["ansible.builtin.command"]))
                inspections = {task.get("register"): task for task in tasks
                               if task.get("register") in {"lab_container_labels", "lab_network_labels"}}
                self.assertIn("lab_named_container.stdout_lines", inspections["lab_container_labels"]["when"])
                self.assertIn("lab_named_network.stdout_lines", inspections["lab_network_labels"]["when"])
                assertions = str([task.get("ansible.builtin.assert", {}).get("that") for task in tasks])
                self.assertIn("lab_named_container.stdout_lines", assertions)
                self.assertIn("lab_named_network.stdout_lines", assertions)

    def test_existing_compose_must_match_approved_render(self):
        for name in ("site", "rollback"):
            with self.subTest(playbook=name):
                assertions = " ".join(clause for task in self._tasks(name)
                                      for clause in task.get("ansible.builtin.assert", {}).get("that", []))
                self.assertIn("lookup('ansible.builtin.template'", assertions)
                self.assertIn("lab_compose_data.content", assertions)

    def test_rollback_only_removes_three_regular_top_level_files(self):
        tasks = self._tasks("rollback")
        found = next(task for task in tasks if "ansible.builtin.find" in task)
        self.assertIs(found["ansible.builtin.find"]["recurse"], True)
        self.assertIs(found["ansible.builtin.find"]["hidden"], True)
        assertions = " ".join(clause for task in tasks
                              for clause in task.get("ansible.builtin.assert", {}).get("that", []))
        self.assertIn("lab_index_file.stat.isreg", assertions)
        self.assertIn("lab_files.files", assertions)
        self.assertIn("lab_files.matched", assertions)
        self.assertIn("== 3", assertions)

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
                self._assert_local_task_boundary(play["tasks"])

    def _assert_local_task_boundary(self, tasks):
        for task in tasks:
            with self.subTest(task=task.get("name")):
                self.assertIsNot(task.get("become"), True)
                self.assertNotIn("connection", task)
                self.assertNotIn("delegate_to", task)
                self.assertNotIn("remote_user", task)
            for nested in ("block", "rescue", "always"):
                self._assert_local_task_boundary(task.get(nested, []))

    def test_site_preflight_guards_deployment(self):
        tasks = yaml.safe_load((LAB / "playbooks/site.yml").read_text(encoding="utf-8"))[0]["tasks"]
        deploy_at = next(i for i, task in enumerate(tasks) if "deploy" in task.get("tags", []))
        preflight = tasks[:deploy_at]
        source = yaml.safe_dump(preflight)
        self.assertTrue(preflight)
        self.assertTrue(all("preflight" in task.get("tags", []) for task in preflight))
        self.assertIn("ansible-galaxy collection list community.docker", source)
        self.assertIn("lab_collection.stdout", source)
        self.assertIn("docker compose version", source)
        self.assertIn("lab_http_port", source)
        self.assertIn("lab_root", source)
        self.assertIn("marker", source)
        self.assertIn("ansible.builtin.assert", source)
        self.assertIn("lab_compose_file", source)
        self.assertIn("lab_preflight_passed", yaml.safe_dump(tasks[deploy_at]))

    def test_site_deploy_and_verify_use_owned_compose_and_loopback(self):
        tasks = yaml.safe_load((LAB / "playbooks/site.yml").read_text(encoding="utf-8"))[0]["tasks"]
        deploy = [task for task in tasks if "deploy" in task.get("tags", [])]
        verify = [task for task in tasks if "verify" in task.get("tags", [])]
        self.assertTrue(any("ansible.builtin.template" in task for task in deploy))
        compose = [task["community.docker.docker_compose_v2"] for task in deploy
                   if "community.docker.docker_compose_v2" in task]
        self.assertEqual(len(compose), 1)
        self.assertEqual(compose[0]["project_src"], "{{ lab_root }}")
        self.assertEqual(compose[0]["project_name"], "{{ lab_project_name }}")
        self.assertEqual(compose[0]["state"], "present")
        self.assertTrue(any(task.get("ansible.builtin.uri", {}).get("url") ==
                            "http://127.0.0.1:{{ lab_http_port }}/" for task in verify))

    def test_rollback_requires_ownership_before_removal(self):
        tasks = yaml.safe_load((LAB / "playbooks/rollback.yml").read_text(encoding="utf-8"))[0]["tasks"]
        compose_at = next(i for i, task in enumerate(tasks)
                          if "community.docker.docker_compose_v2" in task)
        guards = yaml.safe_dump(tasks[:compose_at])
        self.assertIn("marker", guards)
        self.assertIn("lab_project_name", guards)
        self.assertIn("from_yaml", guards)
        self.assertIn("ansible.builtin.find", guards)
        self.assertIn("ansible.builtin.assert", guards)
        compose = tasks[compose_at]["community.docker.docker_compose_v2"]
        self.assertEqual(compose["project_src"], "{{ lab_root }}")
        self.assertEqual(compose["project_name"], "{{ lab_project_name }}")
        self.assertEqual(compose["state"], "absent")
        removals = [task["ansible.builtin.file"] for task in tasks if "ansible.builtin.file" in task
                    and task["ansible.builtin.file"].get("state") == "absent"]
        self.assertEqual(removals, [{"path": "{{ lab_root }}", "state": "absent"}])

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
