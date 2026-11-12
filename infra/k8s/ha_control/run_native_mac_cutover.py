"""Drive one approved WSL→VM cutover over the pinned native transports."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from . import native_guest_ssh as guest_ssh, native_stdio_bridge as bridge
from .native_activation_handoff import ActivationDispatcher
from .native_guest_handoff_io import GuestHandoffIO
from .native_windows_activation import WindowsActivationCustody
from .native_windows_ssh import WindowsSsh


FIELDS = {'binding', 'installation_id', 'jump', 'guests', 'known_hosts', 'guest_launcher_sha256',
          'journal_root', 'wsl_journal_path', 'wsl_source_root', 'wsl_manifest_path', 'wsl_boot_sha256', 'wsl_uid',
          'wsl_config_path', 'wsl_config_sha256', 'wsl_runtime_path', 'windows_stage', 'windows_stage_sha256',
          'windows_start_sha256', 'windows_live_sha256'}


def _reason(error):
    message = str(error)
    if isinstance(error, bridge.BridgeRejected) and re.fullmatch(
            r'(?:native|initial|guest|bridge|activation)_[a-z0-9_]{1,100}', message):
        return message
    return 'native_mac_cutover_unconfirmed'


def run(path, config_sha256):
    raw, _ = guest_ssh._checked_file(Path(path), private=True, maximum=65536)
    bridge._need(bridge._hash(config_sha256) and bridge._digest(raw) == config_sha256,
                 'native_mac_cutover_config_unbound')
    config = json.loads(raw, object_pairs_hook=bridge._pairs, parse_constant=bridge._reject_constant)
    bridge._need(type(config) is dict and set(config) == FIELDS, 'native_mac_cutover_config_invalid')
    binding = bridge.Binding(**config['binding'])
    binding.fields()
    jump = guest_ssh.SshEndpoint(**config['jump'])
    guests = {node: guest_ssh.SshEndpoint(**value) for node, value in config['guests'].items()}
    known_hosts = config['known_hosts']
    bridge._need(type(known_hosts) is dict and set(known_hosts) == {'jump', 'A', 'B'},
                 'native_mac_cutover_config_invalid')
    windows = WindowsSsh(jump, known_hosts['jump'])
    guest = guest_ssh.GuestSshDispatcher(binding, jump=jump, guests=guests, known_hosts=known_hosts,
                                         launcher_sha256=config['guest_launcher_sha256'])
    handoff = GuestHandoffIO(guest, config['installation_id'])
    custody = WindowsActivationCustody(windows, handoff, binding, config['installation_id'], config['wsl_journal_path'],
                                       config['wsl_runtime_path'], config['windows_stage'], config['windows_stage_sha256'],
                                       config['windows_start_sha256'], config['windows_live_sha256'])
    journal = bridge.MutationJournal(config['journal_root'])
    journal.assert_unattempted(binding)
    custody.preflight(time.monotonic() + 12)
    dispatcher = ActivationDispatcher(guest, custody)

    def dispatch(request, deadline):
        try:
            return dispatcher(request, deadline)
        except Exception as error:
            print(f'{request.node} {request.verb} {_reason(error)}', file=sys.stderr, flush=True)
            raise

    with windows.wsl_cutover(config['wsl_source_root'], config['wsl_manifest_path'], binding.source_sha256,
                             config['wsl_boot_sha256'], config['wsl_uid'], config['wsl_config_path'],
                             config['wsl_config_sha256'], time.monotonic() + 12) as process:
        channel = bridge.Stdio(process.stdout.fileno(), process.stdin.fileno())
        server = bridge.Server(channel, binding, journal, dispatch)
        server.serve()
        bridge._need(process.wait(timeout=5) == 0, 'native_wsl_cutover_exit_unconfirmed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--config-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        run(args.config, args.config_sha256)
    except (OSError, ValueError, TypeError, KeyError, bridge.BridgeRejected, subprocess.SubprocessError) as error:
        print(_reason(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
