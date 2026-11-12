"""Run one real WSL cutover over a privately pinned, already authenticated stdio channel."""

import argparse
from pathlib import Path
import stat
import sys

from ha_runtime import seven_capture
from ha_runtime.config import SERVICES

from .caddy_runtime import CaddyRuntime
from .controller import TransitionBlocked
from .deployment_admission import DeploymentAdmission
from .legacy_cutover import _pin
from .legacy_writers import _need
from .native_applications import NativeApplications, compose_native_cutover
from .native_cutover import WindowsReactivationSession, WslBootstrapAdmission
from .native_guest_bridge import GuestBridgeRuntime
from .native_host_evidence import EXTERNAL_ACTORS, NativeHostEvidence, WindowsServiceLiveAuthority
from .native_stdio_bridge import Binding, Client, Stdio


FIELDS = {'schema_version', 'binding', 'identity', 'journal_path', 'capture_root', 'bootstrap_sha256',
          'windows', 'deployment', 'active_ip', 'caddy_project', 'mtls_sha256', 'frozen_credentials',
          'max_frozen_bytes', 'converter_source_pins'}
WINDOWS = {'entrypoint', 'entrypoint_sha256', 'driver', 'driver_sha256', 'plan_path',
           'service_binding_path', 'live_authority_source_sha256'}
NATIVE_PLAN = 'reactivation_plan_sha256'
DEPLOY = {'repository', 'workflow_id', 'gh'}


def _config(path, sha256):
    reader = seven_capture.Reader()
    descriptor = seven_capture.descriptor(path)
    _need(descriptor['sha256'] == _pin(sha256), 'native_wsl_input_pin_mismatch')
    value = seven_capture.parse(reader.read(descriptor, limit=64 * 1024))
    _need(type(value) is dict and set(value) == FIELDS and type(value['schema_version']) is int
          and value['schema_version'] == 1 and type(value['windows']) is dict
          and set(value['windows']) in (WINDOWS, WINDOWS | {NATIVE_PLAN})
          and type(value['deployment']) is dict and set(value['deployment']) == DEPLOY, 'native_wsl_input_invalid')
    reader.unchanged()
    return value


def _deployment(value):
    gh = Path(value['gh'])
    _need(gh.is_absolute() and gh.resolve(strict=True) == gh, 'native_wsl_deployment_cli_untrusted')
    for entry in (gh, *gh.parents):
        info = entry.lstat()
        _need(info.st_uid == 0 and not info.st_mode & 0o022, 'native_wsl_deployment_cli_untrusted')
    _need(stat.S_ISREG(gh.stat().st_mode), 'native_wsl_deployment_cli_untrusted')
    return DeploymentAdmission(value['repository'], value['workflow_id'], str(gh))


def run(config, config_sha256):
    value = _config(Path(config), config_sha256)
    binding = Binding(**value['binding'])
    binding.fields()
    identity = value['identity']
    _need(type(identity) is dict and identity.get('plan_sha256') == binding.plan_sha256, 'native_wsl_scope_unbound')
    journal = Path(value['journal_path'])
    capture_root = Path(value['capture_root'])
    seven_capture.trusted(journal.parent, directory=True)
    seven_capture.trusted(capture_root, directory=True)
    windows_config = value['windows']
    windows = WindowsReactivationSession.for_n100_wsl(
        windows_config['entrypoint'], windows_config['entrypoint_sha256'], windows_config['driver'],
        windows_config['driver_sha256'], windows_config['plan_path'], windows_config.get(NATIVE_PLAN),
    )
    bootstrap = WslBootstrapAdmission(value['bootstrap_sha256'])
    admission = _deployment(value['deployment'])
    caddy = CaddyRuntime('/mnt/c/personal-server', value['caddy_project'])
    external = {service: sorted(EXTERNAL_ACTORS) for service in SERVICES}
    client = Client(Stdio(0, 1), binding)
    guest = GuestBridgeRuntime(client, journal, value['active_ip'], value['frozen_credentials'], value['max_frozen_bytes'])
    authority = WindowsServiceLiveAuthority(windows_config['service_binding_path'], windows_config['live_authority_source_sha256'],
                                            lambda: guest.service_instance_sha256)
    host = NativeHostEvidence(windows, bootstrap, admission, external, authority)
    applications = NativeApplications(
        capture_root=capture_root, identity=identity, converter_source_pins=value['converter_source_pins'],
        external_writers=external, caddy=caddy, active_ip=value['active_ip'], expected_mtls_sha256=value['mtls_sha256'],
        vm=guest, host=host,
    )
    return compose_native_cutover(journal, windows, admission, bootstrap, applications).run(
        binding.plan_sha256, attempt_id=binding.attempt_id,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--config-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args.config, args.config_sha256)
        return 0 if result['phase'] == 'complete' else 1
    except TransitionBlocked as error:
        reason = str(error)
        print(reason if reason.replace('_', '').isascii() and reason.replace('_', '').isalnum()
              else 'native_wsl_cutover_blocked', file=sys.stderr)
        return 1
    except Exception:
        print('native_wsl_cutover_unconfirmed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
