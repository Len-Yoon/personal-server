"""Run fixed native cutover scripts over the pinned N100 Windows SSH connection."""

import base64
from contextlib import contextmanager
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from . import native_guest_ssh as ssh, native_stdio_bridge as bridge

POWERSHELL = r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
WSL = r'C:\Windows\System32\wsl.exe'


class WindowsSsh:
    def __init__(self, endpoint, known_hosts):
        bridge._need(type(endpoint) is ssh.SshEndpoint, 'native_windows_ssh_endpoint_required')
        endpoint.validate(guest=False)
        self.endpoint = endpoint
        self.known_hosts = Path(known_hosts)

    @contextmanager
    def _connection(self, deadline):
        endpoint = self.endpoint
        stamps = ssh._identity(endpoint, deadline)
        known, stamps[self.known_hosts] = ssh._checked_file(self.known_hosts, private=False, maximum=1048576)
        host = ssh._host_record(known, endpoint, guest=False)
        with tempfile.TemporaryDirectory(prefix='ha-native-win-', dir='/private/tmp') as directory:
            config = ssh._ssh_config(Path(directory), endpoint, None, {endpoint.alias: host})
            bridge._need(ssh._same_files(stamps), 'native_windows_ssh_local_file_changed')
            yield (ssh.SSH, '-F', str(config), '-T', endpoint.alias)
            bridge._need(ssh._same_files(stamps), 'native_windows_ssh_local_file_changed')

    def powershell(self, source, deadline):
        bridge._need(type(source) is str and 0 < len(source.encode('utf-8')) <= 131072,
                     'native_windows_ssh_source_size')
        payload = gzip.compress(source.encode('utf-8'), mtime=0)
        digest = hashlib.sha256(payload).hexdigest()
        loader = (
            f'$b=[IO.BinaryReader]::new([Console]::OpenStandardInput()).ReadBytes({len(payload)});'
            '$h=[Security.Cryptography.SHA256]::Create();'
            'try{$pin=[BitConverter]::ToString($h.ComputeHash($b)).Replace("-", "").ToLowerInvariant()}'
            'finally{$h.Dispose()};'
            f'if($pin -cne "{digest}"){{exit 1}};'
            '$m=[IO.MemoryStream]::new($b);'
            '$z=[IO.Compression.GZipStream]::new($m,[IO.Compression.CompressionMode]::Decompress);'
            '$r=[IO.StreamReader]::new($z,[Text.UTF8Encoding]::new($false,$true));'
            'try{&([ScriptBlock]::Create($r.ReadToEnd()))}finally{$r.Dispose()}'
        )
        encoded = base64.b64encode(loader.encode('utf-16-le')).decode('ascii')
        command = f'{POWERSHELL} -NoLogo -NoProfile -NonInteractive -EncodedCommand {encoded}'
        with self._connection(deadline) as argv:
            response = ssh._exchange((*argv, command), payload, deadline)
        try:
            result = json.loads(response, object_pairs_hook=bridge._pairs, parse_constant=bridge._reject_constant)
        except (ValueError, UnicodeError):
            raise bridge.BridgeRejected('native_windows_ssh_response_unconfirmed') from None
        bridge._need(type(result) is dict, 'native_windows_ssh_response_unconfirmed')
        return result

    @contextmanager
    def wsl_cutover(self, source_root, manifest_path, source_sha256, boot_sha256, uid, config_path, config_sha256, deadline):
        for path in (source_root, manifest_path, config_path):
            bridge._need(type(path) is str and re.fullmatch(r'/home/[A-Za-z0-9_.\/-]+', path)
                         and all(part not in {'', '.', '..'} for part in path[1:].split('/')),
                         'native_wsl_launcher_path_unconfirmed')
        bridge._need(all(map(bridge._hash, (source_sha256, boot_sha256, config_sha256)))
                     and type(uid) is int and 0 < uid < 2**32 - 1, 'native_wsl_launcher_pin_unconfirmed')
        running = self.powershell(
            "$w=Join-Path ([Environment]::SystemDirectory) 'wsl.exe';"
            "$rows=@(& $w --list --running --quiet|ForEach-Object{($_ -replace ([char]0),'').Trim()});"
            "if($LASTEXITCODE -ne 0){exit 1};[ordered]@{running='Ubuntu-24.04' -cin $rows}|ConvertTo-Json -Compress",
            deadline,
        )
        bridge._need(bridge._matches(running, {'running': True}), 'native_wsl_launcher_distribution_not_running')
        loader = base64.b64encode(Path(__file__).with_name('native_wsl_launcher.py').read_bytes()).decode('ascii')
        python = f"import base64;exec(base64.b64decode('{loader}'))"
        command = (f'{WSL} --distribution Ubuntu-24.04 --exec /usr/bin/python3 -I -B -S -c "{python}" '
                   f'{source_root} {source_sha256} {boot_sha256} {uid} {manifest_path} '
                   f'--config {config_path} --config-sha256 {config_sha256}')
        bridge._need(len(command) <= 7900, 'native_wsl_launcher_command_too_long')
        with self._connection(deadline) as argv:
            process = subprocess.Popen((*argv, command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
            try:
                yield process
            finally:
                process.stdin.close()
                process.stdout.close()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


def fixed_script(name, digest, parameters):
    """Load an approved colocated script and pass only scalar, shell-quoted values."""
    bridge._need(name in {'windows-ha-cutover-live-authority.ps1', 'windows-ha-start-initial-service.ps1',
                         'windows-ha-start-lease-observer.ps1', 'windows-ha-stage-initial-activation.ps1'},
                 'native_windows_ssh_script_unapproved')
    source = Path(__file__).with_name(name).read_bytes()
    bridge._need(bridge._hash(digest) and hashlib.sha256(source).hexdigest() == digest,
                 'native_windows_ssh_script_unpinned')
    encoded = base64.b64encode(source).decode('ascii')
    command = f'$s=[Text.UTF8Encoding]::new($false,$true).GetString([Convert]::FromBase64String("{encoded}")); &([ScriptBlock]::Create($s))'
    for key, value in parameters.items():
        limit = 5464 if name == 'windows-ha-stage-initial-activation.ps1' and key == 'IntentBase64' else 512
        bridge._need(re.fullmatch(r'[A-Za-z][A-Za-z0-9]*', key) is not None
                     and (value is True or type(value) is str and 0 < len(value) <= limit and value.isascii()
                          and not any(character in value for character in '\x00\r\n')), 'native_windows_ssh_argument_invalid')
        command += ' -' + key
        if value is not True:
            command += " '" + value.replace("'", "''") + "'"
    return command
