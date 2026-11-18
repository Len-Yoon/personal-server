"""Guest isolation and transition authority boundaries, without any deployment."""
import copy
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'infra/k8s'))
from ha_control import native_guest_config, native_guest_runtime
from ha_control.legacy_writers import TransitionBlocked

spec=importlib.util.spec_from_file_location('ha_guest_package',ROOT/'infra/k8s/ha-package/guest.py')
guest=importlib.util.module_from_spec(spec);spec.loader.exec_module(guest)


def fixture():
    return {'schema_version':1,'source_commit':'a'*40,'installation_id':'b'*64,'node':'A',
        'guest_ip':'192.0.2.10','pg_a':'192.0.2.10','pg_b':'192.0.2.11',
        'images':{name:'fixture.invalid/'+name+'@sha256:'+'c'*64 for name in (*guest.SERVICES,'ha-backup-utility','ha-ingress-utility')},
        'gateway_sha256':'d'*64,'broker_sha256':'e'*64,'docker_gid':999,
        'app_environment':{'car-care-worker':{'CAR_CARE_TELEGRAM_CHAT_ID':'fixture'},
            'homeops-executor':{'HOMEOPS_EXECUTOR_SHARED_SECRET_SHA256':'f'*64,
                'HOMEOPS_DOCKER_MANAGED_SERVICES':'portal-web,book-memo'}}}


class NativeGuestRuntimeTest(unittest.TestCase):
    def pins(self, node="A"):
        return native_guest_runtime.RuntimePins(
            node=node, boot_sha256="1" * 64, installation_id="b" * 64, protected_inventory_sha256="3" * 64,
            engine_id_sha256="4" * 64, renderer_sha256="5" * 64, spec_sha256="6" * 64, inputs_sha256="7" * 64,
            plan_sha256="8" * 64, manifest_sha256="9" * 64, frozen_transfer_sha256="a" * 64,
            restore_receipt_sha256="c" * 64, attempt_id="d" * 64, broker_instance="e" * 64,
            release={"source_commit": "a" * 40, "release_id": "f" * 64, "source_files_sha256": "0" * 64},
            system_id="12345", cluster="test_ha",
        )

    def guard(self, pins, *, transfer_closed=True):
        return {"node": pins.node, "installation_id": pins.installation_id, "attempt_id": pins.attempt_id,
                "plan_sha256": pins.plan_sha256, "manifest_sha256": pins.manifest_sha256,
                "frozen_transfer_sha256": pins.frozen_transfer_sha256,
                "restore_receipt_sha256": pins.restore_receipt_sha256, "legacy_writers_closed": True,
                "target_writes_closed": True, "frozen_transfer_closed": transfer_closed}

    def settlement(self, pins, challenge, *, settled=True):
        return {**native_guest_runtime._runtime_identity(pins), "challenge": challenge,
                "runtime_mutations_excluded": True, "runtime_mutations_settled": settled,
                "target_writes_closed": True, "target_never_opened": True,
                "evidence_sha256": native_guest_runtime._digest(challenge.encode())}

    def test_initial_standby_retirement_only_plans_exact_never_started_app_ids(self):
        module = native_guest_runtime
        pins = replace(self.pins("B"), boot_sha256=module._digest(b"guest-boot"))
        shared = ("boot_sha256", "installation_id", "plan_sha256", "manifest_sha256", "cluster", "system_id",
                  "postgres_id", "retained_postgres_id")
        postgres = SimpleNamespace(**{key: getattr(pins, key) for key in shared},
                                   source_commit=pins.release["source_commit"], attempt_id=None, validate=lambda: None)
        roles = sorted(module.SERVICES | {"ha-broker"})
        owned = {role: {"container_id": f"{index:064x}", "image_id": "fixture-id"}
                 for index, role in enumerate(roles, 1)}
        zero = "0001-01-01T00:00:00Z"
        objects = {record["container_id"]: {
            "Id": record["container_id"], "Image": record["image_id"], "RestartCount": 0,
            "Config": {"Labels": {"com.docker.compose.service": role, module.ATTEMPT_LABEL: pins.attempt_id}},
            "State": {"Status": "created", "Running": False, "Pid": 0, "StartedAt": zero, "FinishedAt": zero,
                      "ExitCode": 0, "Paused": False, "Restarting": False, "Dead": False, "OOMKilled": False},
            "HostConfig": {"RestartPolicy": {"Name": "no"}},
        } for role, record in owned.items()}
        database = {"Config": {"Labels": {"com.docker.compose.service": "postgres"}}}
        objects["a"*64] = database
        raw = json.dumps({"containers": owned, "status": "PREPARED", "applications_started": False,
                          "writer_authority_issued": False}).encode()
        pin = module._digest(raw)
        closed = ["ha_B", pins.system_id, True, pins.cluster, 1, "A", "B", 1, 1, False, False, True, "0/20"]
        inventory_pin = lambda items: pins.protected_inventory_sha256 if items == {"a"*64: database} else "e"*64
        with (patch.object(module.os, "geteuid", return_value=0), patch.object(module, "_read", return_value=raw),
              patch.object(module, "_inspect_prepared", return_value=pin), patch.object(module, "_inventory", return_value=objects),
              patch.object(module, "active_postgres"), patch.object(module, "_pg", return_value=closed) as pg,
              patch.object(module, "inventory_sha256", side_effect=inventory_pin), patch.object(module, "_docker") as docker,
              patch.object(module, "BOOT_ID") as boot):
            boot.read_bytes.return_value = b"guest-boot"
            no_activation = Mock()
            options = dict(runtime_receipt_sha256=pin, assert_no_activation=no_activation)
            result = module.observe_initial_standby_retirement(pins, postgres, Path("/stage"), **options)
            self.assertEqual(no_activation.call_count, 2)
            self.assertEqual({record["role"] for record in result["owned"].values()}, set(roles))
            self.assertNotIn("a"*64, result["owned"])
            pg.return_value = closed[:]
            pg.return_value[2] = False
            with self.assertRaisesRegex(TransitionBlocked, "guest_initial_standby_retirement_unconfirmed"):
                module.observe_initial_standby_retirement(pins, postgres, Path("/stage"), **options)
            pg.return_value = closed
            objects[owned[roles[0]]["container_id"]]["State"]["StartedAt"] = "2026-11-18T00:00:00Z"
            with self.assertRaisesRegex(TransitionBlocked, "guest_initial_standby_retirement_unconfirmed"):
                module.observe_initial_standby_retirement(pins, postgres, Path("/stage"), **options)
            docker.assert_not_called()

    def test_standby_retirement_resumes_from_absence_without_resending(self):
        module = native_guest_runtime
        pins = replace(self.pins("B"), boot_sha256=module._digest(b"guest-boot"))
        shared = ("boot_sha256", "installation_id", "plan_sha256", "manifest_sha256", "cluster",
                  "system_id", "postgres_id", "retained_postgres_id")
        postgres = SimpleNamespace(**{key: getattr(pins, key) for key in shared},
                                   source_commit=pins.release["source_commit"], attempt_id=None, validate=lambda: None)
        owned = {f"{index:064x}": {"role": role, "inspect_sha256": "e"*64}
                 for index, role in enumerate(sorted(module.SERVICES | {"ha-broker"}), 1)}
        database_id = "a"*64
        database = {"Config": {"Labels": {"com.docker.compose.service": "postgres"}}}
        objects = {database_id: database, **{key: {} for key in owned}}
        plan = {"node": "B", "attempt_id": pins.attempt_id, "installation_id": pins.installation_id,
                "runtime_receipt_sha256": "9"*64, "owned": owned,
                "protected_inventory_sha256": pins.protected_inventory_sha256}
        approval = module._digest(module._json_bytes(plan))
        first = min(owned)

        def remove(action, identifier):
            self.assertEqual(action, "rm")
            self.assertIn(identifier, owned)
            if identifier == first:
                raise subprocess.TimeoutExpired("docker rm", 1)
            del objects[identifier]
            return identifier.encode()

        def fingerprint(items):
            return pins.protected_inventory_sha256 if items == {database_id: database} else "e"*64

        with (tempfile.TemporaryDirectory() as root, patch.object(module.os, "geteuid", return_value=0),
              patch.object(module, "_directory"), patch.object(module, "BOOT_ID") as boot,
              patch.object(module, "_read", side_effect=lambda path, *_: path.read_bytes()),
              patch.object(module, "observe_initial_standby_retirement", return_value=plan) as observe,
              patch.object(module, "_assert_initial_standby_closed"), patch.object(module, "active_postgres"),
              patch.object(module, "_inventory", side_effect=lambda: dict(objects)),
              patch.object(module, "inventory_sha256", side_effect=fingerprint),
              patch.object(module, "_docker", side_effect=remove) as docker):
            boot.read_bytes.return_value = b"guest-boot"
            journal = Path(root) / "initial-standby-retirement"
            journal.mkdir(mode=0o700)  # Crash before plan publication sent no Docker command.
            options = dict(runtime_receipt_sha256=plan["runtime_receipt_sha256"], plan_sha256=approval,
                           assert_no_activation=Mock())
            for _ in range(2):
                with self.assertRaisesRegex(TransitionBlocked, "guest_initial_standby_removal_unsettled"):
                    module.retire_initial_standby_containers(pins, postgres, root, **options)
            docker.assert_called_once_with("rm", first)
            del objects[first]  # Delayed completion is observed without replay.
            result = module.retire_initial_standby_containers(pins, postgres, root, **options)
            self.assertEqual(result["count"], 8)
            self.assertEqual(objects, {database_id: database})
            self.assertEqual(docker.call_count, 8)
            observe.assert_called_once()

    def test_standby_rebind_recovers_after_atomic_replacement_interruption(self):
        module = native_guest_runtime
        pins = replace(self.pins("B"), boot_sha256=module._digest(b"guest-boot"))
        shared = ("boot_sha256", "installation_id", "plan_sha256", "manifest_sha256", "cluster",
                  "system_id", "postgres_id", "retained_postgres_id")
        postgres = SimpleNamespace(**{key: getattr(pins, key) for key in shared},
                                   source_commit=pins.release["source_commit"], attempt_id=None, validate=lambda: None)
        roles = sorted(module.SERVICES | {"ha-broker"})
        owned = {f"{index:064x}": {"role": role} for index, role in enumerate(roles, 1)}
        receipt = "9"*64
        plan = {"node": "B", "attempt_id": pins.attempt_id, "installation_id": pins.installation_id,
                "runtime_receipt_sha256": receipt, "owned": owned,
                "protected_inventory_sha256": pins.protected_inventory_sha256}
        plan_pin = module._digest(module._json_bytes(plan))
        cert = "e"*64
        real_replace = module.os.replace
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            journal = root / "initial-standby-retirement"
            journal.mkdir()
            (journal / "plan.json").write_bytes(module._json_bytes(plan))
            for identifier in owned:
                (journal / (identifier + ".remove.json")).write_bytes(
                    module._json_bytes({"plan_sha256": plan_pin, "container_id": identifier}))
            (root / "broker").mkdir()
            (root / "broker" / "broker.crt").write_bytes(b"certificate")
            before = {}
            for service in module.SERVICES:
                path = root / "apps" / service
                path.mkdir(parents=True)
                old, passfile = native_guest_config.application_config(
                    cluster=pins.cluster, release=pins.release, plan_sha256=pins.plan_sha256, node="B",
                    installation_id=pins.installation_id, system_id=pins.system_id, service=service,
                    password="p"*32, broker_server_sha256=cert,
                )
                before[service] = (module._json_bytes(old), passfile)
                (path / "config.json").write_bytes(before[service][0])
                (path / "postgres.pgpass").write_bytes(passfile)
            with (patch.object(module.os, "geteuid", return_value=0), patch.object(module.os, "fchown"),
                  patch.object(module, "ROOT", root), patch.object(module, "BOOT_ID") as boot,
                  patch.object(module, "_read", side_effect=lambda path, *_: path.read_bytes()),
                  patch.object(module, "_cert_digest", return_value=cert),
                  patch.object(module, "_assert_initial_standby_closed"), patch.object(module, "active_postgres"),
                  patch.object(module, "_inventory", return_value={}),
                  patch.object(module, "inventory_sha256", return_value=pins.protected_inventory_sha256),
                  patch.object(module, "_docker") as docker):
                boot.read_bytes.return_value = b"guest-boot"
                options = dict(runtime_receipt_sha256=receipt, plan_sha256=plan_pin, assert_no_activation=Mock())
                writes = 0

                def interrupt(src, dst):
                    nonlocal writes
                    if dst.name == "config.json":
                        writes += 1
                        if writes == 2:
                            raise OSError("fixture interruption")
                    return real_replace(src, dst)

                with patch.object(module.os, "replace", side_effect=interrupt):
                    with self.assertRaisesRegex(OSError, "fixture interruption"):
                        module.rebind_initial_standby_application_configs(pins, postgres, root, **options)
                result = module.rebind_initial_standby_application_configs(pins, postgres, root, **options)
                docker.assert_not_called()
            self.assertEqual(result["status"], "APP_CONFIGS_REBOUND")
            self.assertEqual(set(result["config_sha256"]), module.SERVICES)
            for service, (old, passfile) in before.items():
                path = root / "apps" / service
                current = json.loads((path / "config.json").read_bytes())
                self.assertEqual((current["generation"], current["primary_node"], current["primary"]["host"]),
                                 (2, "B", "ha-b"))
                self.assertEqual((path / "postgres.pgpass").read_bytes(), passfile)
                self.assertEqual((journal / (service + ".generation-1-config.json")).read_bytes(), old)

    def test_standby_clears_only_empty_ingress_after_preserving_original_broker(self):
        module = native_guest_runtime
        pins = self.pins("B")
        broker_raw = b'{"generation":1}'
        broker_pin = module._digest(broker_raw)
        configs = {}
        plan_pin = "a"*64
        proof = {"status": "APP_CONFIGS_REBOUND", "plan_sha256": plan_pin, "node": "B",
                 "generation": 2, "config_sha256": configs}
        real_rmdir = module.os.rmdir
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            journal = root / "journal"
            journal.mkdir()
            (root / "broker").mkdir()
            broker = root / "broker" / "config.json"
            broker.write_bytes(broker_raw)
            prepared = module._json_bytes({"broker_sha256": broker_pin})
            (root / "runtime-prepared.json").write_bytes(prepared)
            ingress = root / "ingress"
            ingress.mkdir()
            for service in module.SERVICES:
                (ingress / service).mkdir()
                folder = root / "apps" / service
                folder.mkdir(parents=True)
                config_bytes = module._json_bytes({"node": "B", "service": service, "generation": 2,
                                                  "primary_node": "B", "primary": {"host": "ha-b"},
                                                  "partner": {"host": "ha-a"}})
                (folder / "config.json").write_bytes(config_bytes)
                configs[service] = module._digest(config_bytes)
            with (patch.object(module, "ROOT", root), patch.object(module, "RUN", root),
                  patch.object(module, "_initial_retirement_scope"), patch.object(module, "_directory"),
                  patch.object(module, "_initial_retired_guard", return_value=(journal, Mock())),
                  patch.object(module, "_read", side_effect=lambda path, *_: path.read_bytes()),
                  patch.object(module.os, "fchown"), patch.object(module, "_assert_ingress_dir",
                      side_effect=lambda path: module._need(not any(path.iterdir()), "fixture socket not empty"))):
                options = dict(runtime_receipt_sha256=module._digest(prepared), plan_sha256=plan_pin,
                               rebind_receipt=proof, assert_no_activation=Mock())
                service = min(module.SERVICES)
                config_path = root / "apps" / service / "config.json"
                original = config_path.read_bytes()
                old = module._json_bytes(json.loads(original) | {"generation": 1})
                config_path.write_bytes(old)
                configs[service] = module._digest(old)
                with self.assertRaisesRegex(TransitionBlocked, "guest_initial_standby_paths_unconfirmed"):
                    module.clean_initial_standby_runtime_paths(pins, None, root, **options)
                self.assertEqual(broker.read_bytes(), broker_raw)
                config_path.write_bytes(original)
                configs[service] = module._digest(original)
                leftover = ingress / service / "socket"
                leftover.touch()
                with self.assertRaisesRegex(TransitionBlocked, "socket not empty"):
                    module.clean_initial_standby_runtime_paths(pins, None, root, **options)
                self.assertEqual(broker.read_bytes(), broker_raw)
                leftover.unlink()

                def interrupt(path, *, dir_fd):
                    if path == sorted(module.SERVICES)[1]:
                        raise OSError("fixture removal interruption")
                    real_rmdir(path, dir_fd=dir_fd)

                with patch.object(module.os, "rmdir", side_effect=interrupt):
                    with self.assertRaisesRegex(OSError, "removal interruption"):
                        module.clean_initial_standby_runtime_paths(pins, None, root, **options)
                result = module.clean_initial_standby_runtime_paths(pins, None, root, **options)
                self.assertEqual(result, {"status": "RUNTIME_PATHS_CLEARED", "node": "B", "plan_sha256": plan_pin,
                                          "broker_backup_sha256": broker_pin, "ingress_count": 7})
                self.assertFalse(broker.exists())
                self.assertFalse(any(ingress.iterdir()))
                self.assertEqual((journal / "generation-1-broker-config.json").read_bytes(), broker_raw)

    def test_preflight_reports_frozen_socket_occupancy_without_mutating(self):
        module = native_guest_runtime
        pins = self.pins()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "ingress/portal-web").mkdir(parents=True)
            with (patch.object(module.os, "geteuid", return_value=0), patch.object(module, "RUN", root),
                  patch.object(module, "_preflight") as preflight, patch.object(module, "_write") as write):
                result = module.preflight_runtime(pins, root)
                self.assertEqual(result["status"], "PREFLIGHT_PASS")
                self.assertFalse(result["frozen_socket_released"])
                preflight.assert_called_once_with(pins, root)
                write.assert_not_called()

    def test_prepares_only_stopped_apps_and_broker_after_frozen_transfer(self):
        module = native_guest_runtime
        for node in ("A", "B"):
            with self.subTest(node=node), tempfile.TemporaryDirectory() as directory:
                pins = self.pins(node)
                stage = Path(directory).resolve()
                spec = fixture()
                spec["node"] = node
                compose = guest.render(spec)[0]
                images = {name: {"id": "sha256:" + "c" * 64, "environment": {}} for name in spec["images"]}
                apps = {name: {"container_id": f"{index:064x}", "image_id": images[name]["id"]}
                        for index, name in enumerate(guest.SERVICES, 1)}
                owned = {**apps, "ha-broker": {"container_id": "8" * 64,
                                                "image_id": images["ha-backup-utility"]["id"]}}
                identity = dict(cluster=pins.cluster, release=pins.release, plan_sha256=pins.plan_sha256,
                                node=node, installation_id=pins.installation_id, system_id=pins.system_id)
                clients = {name: f"{index:064x}" for index, name in enumerate(guest.SERVICES, 1)}
                password = "r" * 48
                broker_pass = "".join(f"{host}:5432:ha:ha_effect_broker:{password}\n"
                                      for host in ("ha-a", "ha-b")).encode()
                staged = (identity, clients, "f" * 64, broker_pass)
                (stage / "ingress").mkdir()
                (stage / "broker").mkdir()
                with (
                    patch.object(module.os, "geteuid", return_value=0),
                    patch.object(module, "RUN", stage),
                    patch.object(module, "ROOT", stage),
                    patch.object(module, "_preflight", return_value=(vars(guest), spec, compose, images, "fixture", staged)),
                    patch.object(module, "_created", side_effect=(apps, owned, owned, owned)),
                    patch.object(module, "_ingress_dirs") as ingress,
                    patch.object(module, "_docker") as docker,
                ):
                    with self.assertRaisesRegex(TransitionBlocked, "live_guard_rejected"):
                        module.prepare_runtime(pins, stage, assert_guard=lambda: self.guard(pins, transfer_closed=False))
                    self.assertFalse((stage / "runtime-intent.json").exists())
                    ingress.assert_not_called()
                    docker.assert_not_called()
                    result = module.prepare_runtime(pins, stage, assert_guard=lambda: self.guard(pins))
                    self.assertFalse(result["applications_started"])
                    self.assertFalse(result["writer_authority_issued"])
                    self.assertEqual(result["containers"], owned)
                    self.assertEqual(json.loads((stage / "runtime-prepared.json").read_bytes()), result)
                    ingress.assert_called_once()
                    calls = [call.args for call in docker.call_args_list]
                    self.assertEqual(len(calls), 2)
                    self.assertTrue(all("create" in call and "start" not in call and "up" not in call for call in calls))
                    apps_spec = json.loads((stage / "apps.json").read_bytes())
                    self.assertEqual(set(apps_spec["services"]), set(guest.SERVICES))
                    self.assertEqual(set(json.loads((stage / "broker.json").read_bytes())["services"]), {"ha-broker"})
                    broker = json.loads((stage / "broker/config.json").read_bytes())
                    self.assertEqual(broker["providers"]["docker"]["targets"],
                                     {name: apps[name] for name in ("portal-web", "book-memo")})
                    self.assertFalse((stage / "broker/authority.json").exists())
                    with self.assertRaisesRegex(TransitionBlocked, "reconciliation_required"):
                        module.prepare_runtime(pins, stage, assert_guard=lambda: self.guard(pins))
                    with (patch.object(module, "_read", side_effect=lambda path, *args: path.read_bytes()),
                          patch.object(module, "_assert_ingress_dir"), patch.object(module, "_write") as write):
                        settle = lambda challenge: self.settlement(pins, challenge)
                        outcome = module.observe_runtime_outcome(pins, stage, assert_settled=settle)
                        self.assertEqual(outcome["status"], "PREPARED_CLOSED")
                        self.assertFalse(outcome["automatic_retry_permitted"])
                        self.assertEqual(outcome["runtime_receipt_sha256"],
                                         module._digest((stage / "runtime-prepared.json").read_bytes()))
                        altered = {**result, "applications_started": True}
                        (stage / "runtime-prepared.json").write_bytes(module._json_bytes(altered))
                        self.assertEqual(module.observe_runtime_outcome(pins, stage, assert_settled=settle)["status"],
                                         "UNKNOWN")
                        write.assert_not_called()
                    self.assertEqual(docker.call_count, 2)

    def test_outcome_never_confirms_clean_while_partial_or_unsettled(self):
        module, pins = native_guest_runtime, self.pins()
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory).resolve()
            with (patch.object(module.os, "geteuid", return_value=0), patch.object(module, "RUN", stage),
                  patch.object(module, "ROOT", stage), patch.object(module, "_preflight"),
                  patch.object(module, "_read", side_effect=lambda path, *args: path.read_bytes()),
                  patch.object(module, "_write") as write, patch.object(module, "_docker") as docker):
                clean = lambda challenge: self.settlement(pins, challenge)
                unsettled = lambda challenge: self.settlement(pins, challenge, settled=False)
                replay = lambda challenge: self.settlement(pins, "0" * 64)
                self.assertEqual(module.observe_runtime_outcome(pins, stage, assert_settled=clean)["status"], "CLEAN")
                for invalid in (unsettled, replay):
                    self.assertEqual(module.observe_runtime_outcome(pins, stage, assert_settled=invalid),
                                     {"status": "UNKNOWN", "automatic_retry_permitted": False})
                (stage / "apps.json").touch()
                self.assertEqual(module.observe_runtime_outcome(pins, stage, assert_settled=clean)["status"], "UNKNOWN")
                (stage / "runtime-intent.json").write_bytes(module._json_bytes(module._runtime_intent(pins)))
                self.assertEqual(module.observe_runtime_outcome(pins, stage, assert_settled=clean)["status"], "UNKNOWN")
                write.assert_not_called()
                docker.assert_not_called()

    def test_image_environment_comes_from_exact_inspected_image(self):
        module = native_guest_runtime
        reference = "sha256:" + "f" * 64
        with patch.object(module, "_docker", return_value=(
                f'{reference} amd64 linux ["PATH=/usr/local/bin:/usr/bin","LANG=C.UTF-8","VALUE=a=b c"]\n'
        ).encode()) as docker:
            self.assertEqual(module._images({"images": {"portal-web": reference}}), {
                "portal-web": {"id": reference, "environment": {
                    "PATH": "/usr/local/bin:/usr/bin", "LANG": "C.UTF-8", "VALUE": "a=b c",
                }},
            })
            self.assertEqual(docker.call_args.args[-1], reference)

    def test_created_container_rejects_runtime_drift(self):
        module = native_guest_runtime
        pins = self.pins()
        compose = guest.render(fixture())[0]
        layout = compose["services"]["portal-web"]
        project = compose["name"]
        image = {"id": "sha256:" + "f" * 64, "environment": {
            "PATH": "/usr/local/bin:/usr/bin", "LANG": "C.UTF-8", "APP_ENV": "fixture-default",
        }}
        environment = {**image["environment"], **layout["environment"]}
        item = {
            "Image": image["id"], "Config": {"User": layout["user"], "Cmd": layout["command"], "Entrypoint": None,
                "Labels": {"com.docker.compose.project": project, "com.docker.compose.service": "portal-web",
                           module.ATTEMPT_LABEL: pins.attempt_id},
                "Env": [f"{key}={value}" for key, value in environment.items()]},
            "State": {"Status": "created", "Running": False, "Pid": 0},
            "HostConfig": {"ReadonlyRootfs": True, "NetworkMode": "none", "Privileged": False, "PidMode": "",
                           "IpcMode": "private", "UTSMode": "",
                           "PidsLimit": 128, "CapDrop": ["ALL"], "CapAdd": None, "RestartPolicy": {"Name": "no"},
                           "SecurityOpt": ["no-new-privileges:true"], "GroupAdd": ["10004"],
                           "PortBindings": None, "Devices": [], "DeviceRequests": None, "DeviceCgroupRules": None,
                           "ExtraHosts": ["ha-broker:127.0.0.4", "ha-a:127.0.0.2", "ha-b:127.0.0.3"],
                           "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=640m,mode=1777"}},
            "NetworkSettings": {"Networks": {"none": {}}}, "RestartCount": 0,
            "Mounts": [{"Type": "bind", "Source": entry["source"], "Destination": entry["target"],
                        "RW": not entry["read_only"], "Propagation": "rprivate"} for entry in layout["volumes"]],
        }
        module._assert_created(item, project, "portal-web", image, layout, pins)
        pid = copy.deepcopy(item)
        pid["HostConfig"]["PidMode"] = "host"
        mount = copy.deepcopy(item)
        mount["Mounts"][0]["RW"] = True
        propagation = copy.deepcopy(item)
        propagation["Mounts"][0]["Propagation"] = "rshared"
        env = copy.deepcopy(item)
        env["Config"]["Env"].append("PYTHONSTARTUP=/unexpected")
        env_default = copy.deepcopy(item)
        env_default["Config"]["Env"].remove("LANG=C.UTF-8")
        env_override = copy.deepcopy(item)
        env_override["Config"]["Env"][2] = "APP_ENV=fixture-default"
        duplicate_env = copy.deepcopy(item)
        duplicate_env["Config"]["Env"].append("LANG=C.UTF-8")
        entrypoint = copy.deepcopy(item)
        entrypoint["Config"]["Entrypoint"] = ["/bin/sh"]
        alias = copy.deepcopy(item)
        alias["HostConfig"]["ExtraHosts"][0] = "ha-broker:127.0.0.5"
        duplicate_alias = copy.deepcopy(item)
        duplicate_alias["HostConfig"]["ExtraHosts"].append("ha-a:127.0.0.2")
        tmpfs = copy.deepcopy(item)
        tmpfs["HostConfig"]["Tmpfs"]["/tmp"] = "rw,nosuid,nodev,size=640m,mode=1777"
        extra_tmpfs = copy.deepcopy(item)
        extra_tmpfs["HostConfig"]["Tmpfs"]["/run"] = "rw,noexec,nosuid,nodev"
        ipc = copy.deepcopy(item)
        ipc["HostConfig"]["IpcMode"] = "host"
        uts = copy.deepcopy(item)
        uts["HostConfig"]["UTSMode"] = "host"
        request = copy.deepcopy(item)
        request["HostConfig"]["DeviceRequests"] = [{"Driver": "nvidia", "Count": -1}]
        rule = copy.deepcopy(item)
        rule["HostConfig"]["DeviceCgroupRules"] = ["c *:* rwm"]
        for drift in (pid, mount, propagation, env, env_default, env_override, duplicate_env, entrypoint, alias,
                      duplicate_alias, tmpfs, extra_tmpfs, ipc, uts, request, rule):
            with self.assertRaises(TransitionBlocked):
                module._assert_created(drift, project, "portal-web", image, layout, pins)


class GuestLayoutTest(unittest.TestCase):
    def test_auth_values_are_private_inputs_and_not_compose_environment(self):
        compose, _, _ = guest.render(fixture())
        self.assertEqual(set(guest.AUTH_INPUTS), {"portal-web", "book-memo", "youtube-memo"})
        for service, layout in compose["services"].items():
            if service not in guest.SERVICES:
                continue
            command = layout["command"]
            if service in guest.AUTH_INPUTS:
                self.assertEqual(command[:4], ["python", "-I", "-B", "-c"])
                self.assertEqual(command[6:8], ["-m", "ha_egress.app_entrypoint"])
                compile(command[4], "<guest-auth-loader>", "exec")
                required, optional = map(set, json.loads(command[5]))
                self.assertTrue(required)
                self.assertTrue((required | optional).isdisjoint(layout["environment"]))
                self.assertIn('"/etc/ha/runtime-auth.json"', command[4])
            else:
                self.assertEqual(command[:3], ["python", "-m", "ha_egress.app_entrypoint"])

    def test_native_credentials_are_node_scoped_and_authority_is_not_generated(self):
        credentials = native_guest_config.new_role_passwords("r" * 48)
        release = {"source_commit": "a" * 40, "release_id": "b" * 64, "source_files_sha256": "c" * 64}
        common = {"cluster": "n100_ha", "release": release, "plan_sha256": "d" * 64}
        init = native_guest_config.initializer_config(**common, broker_instance="e" * 64, passwords=credentials)
        self.assertEqual(init["passwords"]["ha_repl"], "r" * 48)
        self.assertEqual(len(set(init["passwords"].values())), 16)
        guest_identity = dict(common, node="B", installation_id="f" * 64, system_id="123")
        app_args = dict(guest_identity, service="portal-web", password=credentials["portal_web_b"],
                        broker_server_sha256="1" * 64)
        app, passfile = native_guest_config.application_config(**app_args)
        self.assertEqual((app["primary"]["host"], app["partner"]["host"]), ("ha-a", "ha-b"))
        self.assertEqual(app["primary"]["user"], "portal_web_b")
        self.assertEqual(passfile.count(credentials["portal_web_b"].encode()), 2)
        self.assertNotIn(credentials["portal_web_a"].encode(), passfile)
        self.assertNotIn(credentials["portal_web_b"], repr(app))
        self.assertNotIn("authority_kind", app)
        self.assertEqual(app["effects"]["plan_sha256"], init["plan_sha256"])
        clients = {name: str(index) * 64 for index, name in enumerate(sorted(guest.SERVICES), 1)}
        broker_args = dict(
            guest_identity, broker_instance="e" * 64,
            password=credentials["ha_effect_broker"], client_sha256=clients,
            ingress_server_sha256="2" * 64, engine_id="fixture",
            targets={"portal-web": {"container_id": "3" * 64, "image_id": "sha256:" + "4" * 64}},
        )
        broker, broker_passfile = native_guest_config.broker_config(**broker_args)
        self.assertEqual({row["service"] for row in broker["clients"]}, set(guest.SERVICES))
        self.assertTrue(all(row["node"] == "B" for row in broker["clients"]))
        self.assertIn(credentials["ha_effect_broker"].encode(), broker_passfile)
        self.assertNotIn(credentials["ha_effect_broker"], repr(broker))
        self.assertEqual(broker["providers"]["executor"]["services"], ["portal-web"])
        self.assertEqual(broker["providers"]["hyundai"], "/etc/ha-broker/providers/hyundai.json")
        next_app, next_passfile = native_guest_config.application_config(**app_args, primary_node="B", generation=2)
        next_broker, next_broker_passfile = native_guest_config.broker_config(**broker_args, primary_node="B", generation=2)
        self.assertEqual((next_app["generation"], next_app["primary_node"], next_app["primary"]["host"],
                          next_app["partner"]["host"]), (2, "B", "ha-b", "ha-a"))
        self.assertEqual((next_broker["generation"], next_broker["postgres"]["primary"]["host"],
                          next_broker["postgres"]["peer"]["host"]), (2, "ha-b", "ha-a"))
        self.assertTrue(all(row["generation"] == 2 and row["node"] == "B" for row in next_broker["clients"]))
        self.assertEqual((next_passfile, next_broker_passfile), (passfile, broker_passfile))
        self.assertNotIn("authority_kind", next_app)
        for build, args in ((native_guest_config.application_config, app_args),
                            (native_guest_config.broker_config, broker_args)):
            with self.assertRaisesRegex(TransitionBlocked, "guest_activation_generation_invalid"):
                build(**args, primary_node="B", generation=1)

    def test_seven_apps_have_only_fixed_capabilities_and_no_external_network(self):
        value=fixture();original=copy.deepcopy(value)
        compose,haproxy,review=guest.render(value)
        self.assertEqual(value,original)
        self.assertEqual(set(compose['services']),set(guest.SERVICES)|{'ha-network','ha-broker','ha-ingress'})
        for name in guest.SERVICES:
            app=compose['services'][name]
            self.assertEqual(app['network_mode'],'none')
            self.assertTrue(app['read_only']);self.assertEqual(app['cap_drop'],['ALL'])
            self.assertEqual(app['user'],'10001:10001');self.assertEqual(app['group_add'],['10004'])
            self.assertNotIn('pid',app);self.assertNotIn('privileged',app);self.assertNotIn('ports',app)
            self.assertNotIn('docker.sock',str(app));self.assertNotIn('/broker',str(app))
            mounts=app['volumes'];self.assertEqual(len(mounts),3)
            self.assertEqual([m['read_only'] for m in mounts],[True,True,False])
            self.assertTrue(all(not m['bind']['create_host_path'] for m in mounts))
            self.assertEqual(app['extra_hosts']['ha-broker'],'127.0.0.4')
            self.assertIn('unix@/run/ha-ingress/'+name+'/http.sock',haproxy)
        self.assertIn('size=640m',compose['services']['portal-web']['tmpfs'][0])
        self.assertFalse(review['ready_for_service_transition'])
        self.assertFalse(review['operating_authority_issued'])
        self.assertFalse(review['production_deployed'])
        self.assertIn('fresh DB lease',review['ingress_health'])

    def test_trusted_components_are_separate_and_private_keys_not_app_mounted(self):
        compose,haproxy,review=guest.render(fixture());services=compose['services']
        self.assertEqual(services['ha-broker']['user'],'10002:10002')
        self.assertEqual(services['ha-network']['user'],'10003:10001')
        self.assertEqual(services['ha-ingress']['user'],'10004:10004')
        self.assertEqual(services['ha-ingress']['entrypoint'],['/usr/sbin/haproxy'])
        self.assertEqual(services['ha-ingress']['command'],['-W','-db','-f','/etc/ha-ingress/haproxy.cfg'])
        self.assertIn('verify required',haproxy);self.assertIn('ssl-min-ver TLSv1.3',haproxy)
        self.assertIn('  no log',haproxy);self.assertNotIn('stats socket',haproxy);self.assertNotIn('daemon',haproxy)
        self.assertNotIn('0.0.0.0',haproxy)
        self.assertIn('bind 192.0.2.10:10443 ssl',haproxy)
        self.assertIn('http-request deny deny_status 403 unless',haproxy)
        self.assertIn('req.hdr_cnt(host) eq 1',haproxy)
        self.assertEqual(haproxy.count('  use_backend '),7)
        self.assertEqual(haproxy.count('  server app unix@'),7)
        self.assertEqual(review['ingress_key_requirements']['server.pem']['mode'],'0400')
        self.assertFalse(review['ingress_key_requirements']['application_mounts_contain_ingress_keys'])
        self.assertEqual(services['ha-broker']['extra_hosts']['ha-a'],'192.0.2.10')
        for service in guest.SERVICES:
            self.assertEqual(services['ha-broker']['extra_hosts'][service+'.ha.internal'],'192.0.2.10')
        self.assertTrue(all(s['restart']=='no' for s in services.values()))
        self.assertEqual(len(review['directories']),15)

    def test_standalone_ingress_matches_full_guest_layout(self):
        value = fixture()
        compose, expected, _ = guest.render(value)
        self.assertEqual(guest.render_ingress(value['guest_ip']), expected)
        image = value['images']['ha-ingress-utility']
        self.assertEqual(guest.render_ingress_container(image), compose['services']['ha-ingress'])
        with self.assertRaises(guest.LayoutError):
            guest.render_ingress('0.0.0.0')
        with self.assertRaises(guest.LayoutError):
            guest.render_ingress_container('haproxy:latest')

    def test_public_host_is_preserved_only_for_its_matching_tls_service(self):
        haproxy = guest.render_ingress(fixture()['guest_ip'])
        self.assertIn('acl sni_portal-web ssl_fc_sni,lower -m str portal-web.ha.internal', haproxy)
        self.assertIn('portal-web.ha.internal:10443 len.pe.kr portfolio.len.pe.kr file.len.pe.kr admin.len.pe.kr', haproxy)
        self.assertIn('use_backend app_portal-web if sni_portal-web host_portal-web', haproxy)
        self.assertIn('use_backend app_book-memo if sni_book-memo host_book-memo', haproxy)
        self.assertNotIn('portal.len.pe.kr', haproxy)

    def test_caddy_candidate_preserves_approved_native_crlf_and_http_host(self):
        from ha_control.caddy_cutover import render
        from ha_control.controller import TransitionBlocked

        source = (ROOT / 'caddy/Caddyfile').read_bytes().replace(b'\n', b'\r\n')
        digest = hashlib.sha256(source).hexdigest()
        candidate = render(source, digest, fixture()['guest_ip'])
        self.assertEqual(candidate.count(b'header_up Host {hostport}'), 7)
        self.assertNotIn(b'\n', candidate.replace(b'\r\n', b''))
        self.assertIn(b'redir https://len.pe.kr{uri} permanent', candidate)
        with self.assertRaisesRegex(TransitionBlocked, 'caddy_cutover_source_unconfirmed'):
            render(source + b'\r\n', digest, fixture()['guest_ip'])

    def test_caddy_roundtrip_requires_exact_current_routes(self):
        from ha_control.caddy_cutover import render, retarget

        source = (ROOT / 'caddy/Caddyfile').read_bytes()
        pin = hashlib.sha256(source).hexdigest()
        a, b = "192.168.100.2", "192.168.100.3"
        active = render(source, pin, a)
        candidate = retarget(source, pin, active, a, b)
        self.assertEqual(candidate.count(f"https://{b}:10443".encode()), 7)
        self.assertEqual(retarget(source, pin, candidate, b, a), active)
        with self.assertRaisesRegex(TransitionBlocked, "caddy_failover_active_routes_changed"):
            retarget(source, pin, active + b"\n", a, b)
        with self.assertRaisesRegex(TransitionBlocked, "caddy_failover_target_unchanged"):
            retarget(source, pin, active, a, a)

    def test_unpinned_images_secret_injection_and_namespace_bypass_are_rejected(self):
        changes=[lambda v:v['images'].update({'portal-web':'portal:latest'}),
            lambda v:v['app_environment']['car-care-worker'].update({'CAR_CARE_TELEGRAM_BOT_TOKEN':'secret'}),
            lambda v:v['app_environment']['homeops-executor'].update({'PYTHONPATH':'/tmp'}),
            lambda v:v.update({'network_mode':'host'}),lambda v:v.update({'docker_gid':10001}),
            lambda v:v.update({'pg_b':v['pg_a']}),lambda v:v.update({'guest_ip':'127.0.0.1'}),
            lambda v:v['app_environment']['homeops-executor'].update({'HOMEOPS_DOCKER_MANAGED_SERVICES':'car-care-worker'})]
        for change in changes:
            value=fixture();change(value)
            with self.subTest(value=value),self.assertRaises(guest.LayoutError):guest.render(value)

    def test_offline_config_ids_are_exact_and_mutable_or_malformed_references_rejected(self):
        value=fixture()
        value['images']={name:'sha256:'+format(index+1,'064x') for index,name in enumerate(value['images'])}
        compose,_,_=guest.render(value)
        for name in guest.SERVICES:
            self.assertEqual(compose['services'][name]['image'],value['images'][name])
            self.assertEqual(compose['services'][name]['pull_policy'],'never')
        self.assertEqual(compose['services']['ha-network']['image'],value['images']['ha-backup-utility'])
        self.assertEqual(compose['services']['ha-broker']['image'],value['images']['ha-backup-utility'])
        self.assertEqual(compose['services']['ha-ingress']['image'],value['images']['ha-ingress-utility'])
        for invalid in ('portal:latest','portal:fixed','a'*64,'sha256:abc','sha256:'+'g'*64,
                        'sha256:'+'A'*64,'sha512:'+'a'*64,'sha256:'+'a'*65,'sha256:'+'a'*64+'\n'):
            changed=fixture();changed['images']['portal-web']=invalid
            with self.subTest(image=invalid),self.assertRaises(guest.LayoutError):guest.render(changed)

    def test_original_argv_comes_from_actual_seven_dockerfiles(self):
        compose,_,_=guest.render(fixture())
        import json
        for service in guest.SERVICES:
            lines=(ROOT/service/'Dockerfile').read_text().splitlines()
            original=json.loads(next(line[4:] for line in lines if line.startswith('CMD ')))
            command=compose['services'][service]['command']
            self.assertEqual(command[command.index('--')+1:],original)


class IngressPackageTest(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('ha_ingress_package',ROOT/'infra/k8s/ha-package/ingress.py')
        self.ingress=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.ingress)
        self.files={name:(ROOT/name).read_bytes() for name in self.ingress.SOURCES}

    def test_signed_pinned_haproxy_has_fixed_foreground_command_and_inventory(self):
        recipe,base=self.ingress.recipe(self.files)
        self.assertIn('apk add --no-cache --upgrade',recipe)
        for package in ('haproxy=3.4.7-r0','musl=1.2.6-r3','musl-utils=1.2.6-r3','zlib=1.4-r0','libssl3=3.5.9-r0','libcrypto3=3.5.9-r0'):
            self.assertIn(package,recipe)
        self.assertIn('apk info -v | LC_ALL=C sort',recipe)
        self.assertIn('python:3.11-alpine3.24@sha256:',base)
        self.assertIn('USER 10004:10004',recipe)
        self.assertIn('ENTRYPOINT ["/usr/sbin/haproxy"]',recipe)
        self.assertIn('CMD ["-W", "-db", "-f", "/etc/ha-ingress/haproxy.cfg"]',recipe)
        self.assertNotIn('EXPOSE',recipe);self.assertNotIn('allow-untrusted',recipe)
        altered=dict(self.files);altered[self.ingress.SOURCES[1]]+=b'RUN arbitrary\n'
        with self.assertRaises(self.ingress.P.PackageError):self.ingress.recipe(altered)

    def test_prepare_binds_exact_git_and_detects_rendered_or_manifest_tampering(self):
        import tempfile,subprocess,json
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'repo';root.mkdir()
            for name,raw in self.files.items():
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
            def git(*args):return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL)
            git('init','-q');git('add','.');git('-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture')
            commit=git('rev-parse','HEAD').decode().strip();output=Path(temp)/'bundle'
            with patch.object(self.ingress.P,'ROOT',root):
                manifest=self.ingress.prepare(commit,output)
                self.assertEqual(self.ingress.verify_source(output),manifest)
                path=output/'Dockerfile';path.chmod(0o600);original=path.read_bytes();path.write_bytes(original+b'RUN arbitrary\n')
                with self.assertRaises(self.ingress.P.PackageError):self.ingress.verify_source(output)
                path.write_bytes(original)
                path=output/'source-manifest.json';path.chmod(0o600);value=json.loads(path.read_bytes());value['source_commit']='f'*40;path.write_text(json.dumps(value))
                with self.assertRaises(self.ingress.P.PackageError):self.ingress.verify_source(output)
