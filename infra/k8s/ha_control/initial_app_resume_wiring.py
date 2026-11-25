"""Explicit successor app startup using native authority and authenticated SSH.

Importing this module performs no I/O. A caller must own an exclusive durable
journal, pin the successor publication, and reconcile any incomplete dispatch.
Seven applications and their broker are started by the existing guest worker;
this adapter never creates containers or changes database authority.
"""
import base64
from dataclasses import dataclass
import inspect
import json
import math
from pathlib import Path, PureWindowsPath
import re
import shlex
import sys
import tempfile
import time

from . import native_guest_ssh as ssh, native_stdio_bridge as wire
from .controller import TransitionBlocked
from .initial_postboot import scope, published_postboot, TRANSITION_FIELDS
from .initial_unpublished import derive_predecessor

ERROR = 'successor_app_start_unverified'


def need(condition):
    if not condition:
        raise TransitionBlocked(ERROR)


def _snapshot_prior_resume(value):
    if value is None:
        return None
    need(type(value) is dict and set(value) == {'authorization_sha256', 'intent_sha256', 'progress_sha256'}
         and wire._hash(value['authorization_sha256']) and wire._hash(value['intent_sha256'])
         and type(value['progress_sha256']) is dict
         and all(type(key) is str and wire._hash(pin) for key, pin in value['progress_sha256'].items()))
    return json.loads(wire._bytes(value))


def sample_pair(plan, plan_pin, terminal_raw, challenge, incarnation, *, connect=None):
    """Read both TLS PostgreSQL nodes, retaining each original sample's expiry.

    This function is also sent verbatim to the pinned Windows Python runtime.
    It has no write SQL, retry, role transition, or external effect operation.
    """
    import datetime
    import hashlib
    import json
    import math
    import re
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import ExitStack
    from ha_control.initial_pg_lease import InitialPgLeaseRecovery
    from ha_control.initial_postboot import scope, published_postboot
    from ha_control.initial_unpublished import derive_predecessor
    from ha_runtime.effects import Binding, validate_catalog

    def require(value):
        if not value:
            raise ValueError('successor_pair_unverified')

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    postboot = scope(plan)
    require(published_postboot(postboot) and set(plan['pg_nodes']) == {'A', 'B'})
    terminal_pin = postboot['predecessor_terminal_sha256']
    _, revision, _ = derive_predecessor(terminal_raw, terminal_pin, published=True)
    prior = json.loads(terminal_raw)
    require(prior['recovery'] == plan['initial_cutover_recovery']
            and prior['attempt_id'] == plan['initial_cutover_handoff']['attempt_id']
            and prior['legacy_plan_sha256'] == plan['initial_cutover_handoff']['legacy_plan_sha256'])
    require(all(type(pin) is str and re.fullmatch('[a-f0-9]{64}', pin) for pin in (plan_pin, challenge, incarnation)))
    expected = plan['pg_enforcement']['effects']['deployment_binding']
    require(expected['plan_sha256'] == prior['legacy_plan_sha256'])
    quarantine = InitialPgLeaseRecovery.quarantine_binding(plan)
    binding = Binding(plan['cluster'], str(plan['system_id']), expected['source_commit'], expected['plan_sha256'],
                      plan['pg_enforcement']['effects']['broker_role'], broker_instance='0' * 64)
    if connect is None:
        import psycopg
        connect = psycopg.connect
    prefix = 'postboot-a:' + postboot['attempt_id'] + ':'
    beginning = time.monotonic()
    barrier = threading.Barrier(2)

    def lsn(value):
        require(type(value) is str and re.fullmatch('[0-9A-F]+/[0-9A-F]+', value))
        high, low = value.split('/')
        return (int(high, 16) << 32) + int(low, 16)

    def sample(node, connection):
        connection.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        barrier.wait(timeout=.7)
        sampled_at = time.monotonic() - beginning
        row = connection.execute("""SELECT c.cluster,(pg_control_system()).system_identifier::text,
            c.epoch,c.revision,c.timeline,c.primary_node,c.partner,c.serving,c.worker,c.mode,
            c.writes_open,c.history_safe,c.expires_at,extract(epoch FROM c.expires_at-clock_timestamp()),
            pg_is_in_recovery(),current_setting('cluster_name'),current_setting('synchronous_commit'),
            current_setting('fsync'),current_setting('full_page_writes'),
            CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn() ELSE pg_current_wal_flush_lsn() END::text,
            regexp_replace(current_setting('synchronous_standby_names'),'[[:space:]\"]','','g'),
            (SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()),current_setting('transaction_read_only'),
            CASE WHEN pg_is_in_recovery() THEN (SELECT received_tli FROM pg_stat_wal_receiver)
            ELSE ('x'||substring(pg_walfile_name(pg_current_wal_insert_lsn()),1,8))::bit(32)::integer END,
            host(inet_server_addr()) FROM ha.control c WHERE singleton=1""").fetchone()
        require(type(row) is tuple and len(row) == 25)
        require(row[:12] == (plan['cluster'], str(plan['system_id']), 1, revision, 1,
                            'A', 'B', ['A'], 'A', 'AS', True, True))
        require(isinstance(row[12], datetime.datetime) and row[12].utcoffset() is not None)
        remaining = float(row[13])
        require(math.isfinite(remaining) and 1 < remaining <= 5
                and row[14:19] == (node == 'B', 'ha_' + node, 'remote_apply', 'on', 'on')
                and row[20:24] == ('FIRST1(ha_b)' if node == 'A' else 'FIRST1(ha_a)', True, 'on', 1))
        nonce_row = connection.execute("""SELECT array(SELECT nonce FROM ha.controller_probes
            WHERE generation=1 AND left(nonce,%s)=%s AND right(nonce,6)=':lease'
            ORDER BY nonce LIMIT 2)""", (len(prefix), prefix)).fetchone()
        require(type(nonce_row) is tuple and len(nonce_row) == 1 and type(nonce_row[0]) is list
                and len(nonce_row[0]) == 1 and type(nonce_row[0][0]) is str
                and re.fullmatch(re.escape(prefix) + '[a-f0-9]{32}:lease', nonce_row[0][0]))
        require(connection.execute('SHOW transaction_read_only').fetchone() == ('on',))
        validate_catalog(connection, binding)
        require(connection.execute('SELECT cluster,system_id,source_commit,plan_sha256,broker_role FROM ha_effects.binding').fetchall()
                == [(plan['cluster'], str(plan['system_id']), expected['source_commit'], expected['plan_sha256'],
                     plan['pg_enforcement']['effects']['broker_role'])])
        require(InitialPgLeaseRecovery._quarantine_observation(connection, renewal=True)
                == (quarantine[0], quarantine[2], quarantine[3]))
        if node == 'A':
            replication = connection.execute("""SELECT r.application_name,r.usename,host(r.client_addr),
                r.state,r.sync_state,s.ssl,r.replay_lsn::text FROM pg_stat_replication r
                JOIN pg_stat_ssl s USING(pid)""").fetchall()
        else:
            replication = connection.execute('SELECT status,received_tli FROM pg_stat_wal_receiver').fetchall()
        return row, nonce_row[0][0], replication, [sampled_at, remaining]

    with ThreadPoolExecutor(max_workers=2) as pool, ExitStack() as closer:
        def open_connection(node):
            params = plan['pg_nodes'][node]
            require(params.get('sslmode') == 'verify-full')
            return connect(**params, connect_timeout=2, autocommit=True, gssencmode='disable',
                           options='-c default_transaction_read_only=on -c statement_timeout=700 -c lock_timeout=500')
        pending = {node: pool.submit(open_connection, node) for node in ('A', 'B')}
        connections, failures = {}, []
        for node in ('A', 'B'):
            try:
                connection = pending[node].result()
            except Exception as error:
                failures.append(error)
            else:
                closer.callback(connection.close)
                connections[node] = connection
        if failures:
            raise ValueError('successor_pair_connection_unverified') from None
        pending = {node: pool.submit(sample, node, connections[node]) for node in ('A', 'B')}
        primary, nonce, peers, a_time = pending['A'].result()
        replica, replica_nonce, receiver, b_time = pending['B'].result()
        require(primary[12] == replica[12] and nonce == replica_nonce
                and len(peers) == 1 and peers[0][:6] == ('ha_b', 'ha_repl', replica[24], 'streaming', 'sync', True)
                and receiver == [('streaming', 1)]
                and 0 < lsn(primary[19]) <= lsn(replica[19]) and lsn(peers[0][6]) >= lsn(primary[19]))
        proof = {'cluster': plan['cluster'], 'epoch': 1, 'revision': revision, 'timeline': 1,
                 'primary': 'A', 'partner': 'B', 'serving': ['A'], 'worker': 'A', 'mode': 'AS',
                 'controller_sha256': plan_pin, 'deployment_sha256': expected['plan_sha256'],
                 'system_id': str(plan['system_id']), 'expires_at': primary[12].isoformat(), 'nonce': nonce,
                 'recovery': plan['initial_cutover_recovery'], 'postboot': postboot}
        lease = digest(proof)
        elapsed = time.monotonic() - beginning
        require(min(a_time[0] + a_time[1], b_time[0] + b_time[1]) - elapsed > 1)
        return {'proof_sha256': digest({'challenge': challenge, 'incarnation': incarnation, 'lease_sha256': lease,
                                       'plan_sha256': plan_pin, 'terminal_sha256': terminal_pin, 'revision': revision,
                                       'expiry': primary[12].isoformat(), 'flush': primary[19], 'replay': replica[19]}),
                'lease_sha256': lease, 'observations': {'A': a_time, 'B': b_time},
                'child_elapsed_seconds': elapsed}


@dataclass(frozen=True)
class PairedProofInputs:
    runtime_path: str
    plan_path: str
    plan_sha256: str
    terminal_path: str
    terminal_sha256: str
    python_sha256: str
    source_commit: str

    def validate(self):
        for value in (self.runtime_path, self.plan_path, self.terminal_path):
            need(type(value) is str and re.fullmatch(r'[A-Za-z]:\\[A-Za-z0-9_.\\-]+', value)
                 and '..' not in PureWindowsPath(value).parts and '.' not in value.split('\\'))
        need(all(wire._hash(value) for value in (self.plan_sha256, self.terminal_sha256, self.python_sha256))
             and type(self.source_commit) is str and re.fullmatch('[a-f0-9]{40}', self.source_commit))


class PairedAuthorityBoundary:
    """Bracket actual paired SQL inside one authenticated Windows invocation."""

    def __init__(self, powershell, inputs, deadline):
        inputs.validate()
        need(callable(powershell) and type(deadline) in (int, float) and math.isfinite(deadline))
        self.call, self.inputs, self.deadline, self.phase = powershell, inputs, deadline, 0

    def powershell(self, source, deadline):
        need(time.monotonic() < deadline <= self.deadline)
        if self.phase == 2:
            need(source == self.source)
            self.phase = 3
            return self.result['after']
        need(self.phase == 0 and type(source) is str and ' -RecoveryContinuity' in source)
        challenges = re.findall(r" -PriorSha256 '([a-f0-9]{64})'", source)
        need(len(challenges) == 1)
        self.source, self.challenge = source, challenges[0]
        cfg = self.inputs
        data = dict(runtime=cfg.runtime_path, plan_path=cfg.plan_path, plan_pin=cfg.plan_sha256,
                    terminal_path=cfg.terminal_path, terminal_pin=cfg.terminal_sha256, commit=cfg.source_commit,
                    challenge=self.challenge)
        # Only the reviewed sampler is transmitted; the predecessor runtime's
        # stable catalog and plan validators remain authoritative.
        python = 'import sys,json,hashlib\nfrom pathlib import Path\n'
        python += 'CFG=' + repr(data) + '\n'
        python += "sys.path.insert(0,str(Path(CFG['runtime'])/'controller/files'))\n"
        python += inspect.getsource(sample_pair) + '\n'
        python += """raw=Path(CFG['plan_path']).read_bytes()
terminal=Path(CFG['terminal_path']).read_bytes()
if hashlib.sha256(raw).hexdigest()!=CFG['plan_pin'] or hashlib.sha256(terminal).hexdigest()!=CFG['terminal_pin']:
    raise ValueError('successor_pair_input_pin')
plan=json.loads(raw)
from ha_control.cli import validate_bundle
validate_bundle(plan)
if plan['source_commit']!=CFG['commit'] or plan['initial_recovery_postboot']['predecessor_terminal_sha256']!=CFG['terminal_pin']:
    raise ValueError('successor_pair_plan_scope')
result=sample_pair(plan,CFG['plan_pin'],terminal,CFG['challenge'],sys.argv[1])
print(json.dumps(result,sort_keys=True,separators=(',',':')))
"""
        encoded = base64.b64encode(python.encode()).decode()
        python_path = str(PureWindowsPath(cfg.runtime_path) / 'python' / 'python.exe')
        self.started = time.monotonic()
        budget = deadline - self.started
        need(.3 < budget <= wire.ACTIVATION_BOUNDARY_SECONDS)
        command = f"""
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$clock=[Diagnostics.Stopwatch]::StartNew()
$authority={{ {source} }}
$python='{python_path}'
$file=Get-Item -LiteralPath $python -Force -ErrorAction Stop
if(($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
 (Get-FileHash -LiteralPath $python -Algorithm SHA256).Hash.ToLowerInvariant() -cne '{cfg.python_sha256}'){{throw 'successor_python_pin'}}
$body=[Text.UTF8Encoding]::new($false,$true).GetString([Convert]::FromBase64String('{encoded}'))
$first=@(& $authority)
if($first.Count -ne 1){{throw 'successor_before_shape'}}
$before=ConvertFrom-Json $first[0]
$limit={budget:.6f}
for($readIndex=0;$readIndex -lt 2;$readIndex++){{
 $offset=$clock.Elapsed.TotalSeconds
 $rows=@($body|& $python -I -B - $before.service_incarnation_sha256)
 if($LASTEXITCODE -ne 0 -or $rows.Count -ne 1){{throw 'successor_pair_unverified'}}
 $last=@(& $authority)
 if($last.Count -ne 1){{throw 'successor_after_shape'}}
 $pair=ConvertFrom-Json $rows[0]
 $after=ConvertFrom-Json $last[0]
 # If publication advanced around the paired read, reuse its verified
 # closing snapshot as the opening snapshot for one new read. There is
 # no second SSH command and the caller's deadline still applies.
 $advanced=$after.heartbeat_sequence - $before.heartbeat_sequence
 if($readIndex -ne 0 -or $pair.lease_sha256 -cin @($before.live_lease_sha256,$after.live_lease_sha256) -or
    $pair.lease_sha256 -cnotmatch '\\A[a-f0-9]{{64}}\\z' -or
    $before.service_incarnation_sha256 -cne $after.service_incarnation_sha256 -or
    $advanced -lt 0 -or $advanced -gt 1 -or
    ($advanced -eq 0 -and $before.live_lease_sha256 -cne $after.live_lease_sha256) -or
    $clock.Elapsed.TotalSeconds + 1.8 -ge $limit){{break}}
 foreach($sample in @($before,$after)){{
  if((@($sample.PSObject.Properties.Name|Sort-Object -CaseSensitive) -join ',') -cne
     'heartbeat_sequence,live_lease_sha256,service_incarnation_sha256,windows_generation' -or
     ($sample.windows_generation -isnot [int] -and $sample.windows_generation -isnot [long]) -or
     $sample.windows_generation -ne 1 -or
     ($sample.heartbeat_sequence -isnot [int] -and $sample.heartbeat_sequence -isnot [long]) -or
     $sample.heartbeat_sequence -lt 1 -or
     $sample.live_lease_sha256 -isnot [string] -or $sample.live_lease_sha256 -cnotmatch '\\A[a-f0-9]{{64}}\\z' -or
     $sample.service_incarnation_sha256 -isnot [string] -or $sample.service_incarnation_sha256 -cnotmatch '\\A[a-f0-9]{{64}}\\z'){{
   throw 'successor_retry_native_shape'
  }}
 }}
 $before=$after
}}
@{{before=$before;after=$after;pair=$pair;
 offset=$offset;elapsed=$clock.Elapsed.TotalSeconds}}|ConvertTo-Json -Depth 6 -Compress
"""
        need(len(command.encode()) <= 131072)
        result = self.call(command, deadline)
        need(time.monotonic() < deadline and type(result) is dict
             and set(result) == {'before', 'after', 'pair', 'offset', 'elapsed'}
             and all(type(result[key]) in (int, float) and math.isfinite(result[key]) for key in ('offset', 'elapsed'))
             and 0 <= result['offset'] <= result['elapsed'] < budget)
        self.result, self.phase = result, 1
        return result['before']

    def observe_pair(self, *, challenge, service_incarnation_sha256, deadline):
        need(self.phase == 1 and time.monotonic() < deadline <= self.deadline and challenge == self.challenge)
        before, after, pair = (self.result[key] for key in ('before', 'after', 'pair'))
        need(type(before) is dict and type(after) is dict
             and all(set(value) == {'windows_generation', 'service_incarnation_sha256', 'heartbeat_sequence', 'live_lease_sha256'}
                     and type(value['windows_generation']) is int and value['windows_generation'] == 1
                     and wire._hash(value['live_lease_sha256']) for value in (before, after))
             and before['service_incarnation_sha256'] == after['service_incarnation_sha256'] == service_incarnation_sha256
             and type(before['heartbeat_sequence']) is int and type(after['heartbeat_sequence']) is int
             and 0 < before['heartbeat_sequence'] <= after['heartbeat_sequence']
             and (before['heartbeat_sequence'] != after['heartbeat_sequence']
                  or before['live_lease_sha256'] == after['live_lease_sha256']))
        need(type(pair) is dict and set(pair) == {'proof_sha256', 'lease_sha256', 'observations', 'child_elapsed_seconds'}
             and wire._hash(pair['proof_sha256']) and wire._hash(pair['lease_sha256'])
             and type(pair['observations']) is dict and set(pair['observations']) == {'A', 'B'})
        elapsed = pair['child_elapsed_seconds']
        need(type(elapsed) in (int, float) and math.isfinite(elapsed)
             and 0 <= elapsed <= self.result['elapsed'] - self.result['offset'])
        expiries = []
        for sample in pair['observations'].values():
            need(type(sample) is list and len(sample) == 2
                 and all(type(value) in (int, float) and math.isfinite(value) for value in sample))
            at, remaining = sample
            need(0 <= at <= elapsed and 1 < remaining <= 5)
            expiries.append(self.started + max(0, self.result['offset'] + at - .05) + remaining)
        valid_until = min(expiries)
        need(valid_until - time.monotonic() > 1)
        need(pair['lease_sha256'] in (before['live_lease_sha256'], after['live_lease_sha256']))
        self.phase = 2
        return {'proof_sha256': pair['proof_sha256'], 'lease_sha256': pair['lease_sha256'],
                'valid_until_monotonic': valid_until}


def guest_attest(dispatcher, request, launcher, manifest_sha256, service_instance_sha256,
                 prior_intent_sha256, prior_progress_sha256, deadline, *, assert_guard, prior_resume=None):
    """Run native root-only carryover attestation over pinned SSH, once."""
    need(type(dispatcher) is ssh.GuestSshDispatcher and dispatcher.binding == request.binding
         and type(launcher) is bytes and wire._digest(launcher) == dispatcher.launcher_sha256
         and wire._hash(manifest_sha256) and wire._hash(service_instance_sha256)
         and wire._hash(prior_intent_sha256) and type(prior_progress_sha256) is dict
         and all(type(key) is str and wire._hash(pin) for key, pin in prior_progress_sha256.items())
         and callable(assert_guard))
    prior_resume = _snapshot_prior_resume(prior_resume)
    marker = b'from ha_control.native_guest_worker import main\nraise SystemExit(main())'
    need(launcher.count(marker) == 1 and manifest_sha256.encode() in launcher)
    # Native attest authenticates the carryover receipt and preserves attempt 8.
    data = {'binding': request.binding.fields(), 'node': request.node, 'verb': request.verb,
            'sequence': request.sequence, 'nonce': request.nonce, 'args': request.args,
            'instance': service_instance_sha256, 'intent': prior_intent_sha256, 'progress': prior_progress_sha256,
            'prior_resume': prior_resume}
    code = 'import json\nCFG=' + repr(data) + '\n' + launcher.split(marker)[0].decode() + """
from ha_control import native_stdio_bridge as wire
from ha_control.initial_app_resume import attest
request=wire.Request(wire.Binding(**CFG['binding']),CFG['node'],CFG['verb'],CFG['sequence'],CFG['nonce'],CFG['args'])
result=attest(request,service_instance_sha256=CFG['instance'],prior_intent_sha256=CFG['intent'],prior_progress_sha256=CFG['progress'],**({} if CFG['prior_resume'] is None else {'prior_resume':CFG['prior_resume']}))
print(json.dumps(result,sort_keys=True,separators=(',',':')))
"""
    compile(code, '<successor-app-attestation>', 'exec')
    assert_guard()
    wire._remaining(deadline)
    stamps, records = {}, {}
    for role, endpoint in (('jump', dispatcher.jump), ('A', dispatcher.guests['A'])):
        stamps.update(ssh._identity(endpoint, deadline))
        path = dispatcher.known_hosts[role]
        raw, stamps[path] = ssh._checked_file(path, private=False, maximum=1048576)
        records[endpoint.alias] = ssh._host_record(raw, endpoint, guest=role == 'A')
    root = '/private/tmp' if sys.platform == 'darwin' else '/tmp'
    with tempfile.TemporaryDirectory(prefix='ha-successor-attest-', dir=root) as directory:
        config = ssh._ssh_config(Path(directory), dispatcher.jump, dispatcher.guests['A'], records)
        command = (ssh.SSH, '-F', str(config), '-T', dispatcher.guests['A'].alias,
                   shlex.join(('/usr/bin/python3', '-I', '-B', '-S', '-c', code)))
        need(ssh._same_files(stamps))
        result = json.loads(ssh._exchange(command, b'', deadline), object_pairs_hook=wire._pairs)
        need(ssh._same_files(stamps))
    assert_guard()
    wire._remaining(deadline)
    fields = {'schema_version', 'authorization_sha256', 'request_nonce', 'baseline_sha256', 'prior_intent_sha256',
              'prior_progress_sha256', 'service_instance_sha256', 'container_count', 'containers_started', 'database_changed'}
    need(type(result) is dict and set(result) == fields and type(result['schema_version']) is int
         and result['schema_version'] == 1 and result['request_nonce'] == request.nonce
         and result['prior_intent_sha256'] == prior_intent_sha256 and result['prior_progress_sha256'] == prior_progress_sha256
         and result['service_instance_sha256'] == service_instance_sha256
         and type(result['container_count']) is int and result['container_count'] == 8
         and result['containers_started'] is False and result['database_changed'] is False
         and wire._hash(result['authorization_sha256']) and wire._hash(result['baseline_sha256']))
    return result


_COMPLETION_READ = r'''
import os,time
from ha_control import native_guest_worker as w,initial_app_resume as resume,native_stdio_bridge as wire
deadline=time.monotonic()+CFG['budget']
request=wire.Request(wire.Binding(**CFG['binding']),CFG['node'],CFG['verb'],CFG['sequence'],CFG['nonce'],CFG['args'])
def require(value):
    if not value:raise ValueError('successor_app_completion_unverified')
require(os.geteuid()==0 and request.node=='A' and request.verb==wire.GUEST_START_INITIAL_POSTBOOT)
wire._args(request.verb,request.node,request.args)
require(wire._digest(w.runtime.BOOT_ID.read_bytes())==request.binding.boot('A'))
with w._guest_mutation_descriptor(wait=2):
    raw,hold,prepared,active=w._read_initial_recovery_hold(request)
    stage=w.ROOT/request.binding.attempt_id
    numbered=resume.latest_attempt(request)
    continuation=bool(numbered) or resume.path(request,partial=True,prestart=True,continuation=True).exists()
    prestart=continuation or resume.path(request,partial=True,prestart=True).exists()
    partial=prestart or resume.path(request,partial=True).exists()
    target=resume.path(request,partial=partial,prestart=prestart,continuation=continuation,numbered_attempt=numbered)
    authorization_raw=w.runtime._read(target,0,{0o400},limit=131072)
    authorization=resume.decode(authorization_raw)
    require(wire._digest(authorization_raw)==CFG['authorization']['authorization_sha256'])
    require(authorization['request']==w._scope(request) and authorization['args']==request.args
        and authorization['service_instance_sha256']==CFG['instance']
        and authorization['prior_intent_sha256']==CFG['authorization']['prior_intent_sha256']
        and authorization['prior_progress_sha256']==CFG['authorization']['prior_progress_sha256']
        and authorization.get('prior_resume')==CFG['prior_resume'])
    base=resume.prefix(request)
    stem=(resume._numbered_stem(base,numbered) if numbered else base+'resume-'+
          ('partial-' if partial else '')+('prestart-' if prestart else '')+('continuation-' if continuation else ''))
    order=w.RECOVERY_START_ORDER
    names=['intent.json','done.json']+['progress-'+role+'.json' for role in order]
    journals={name:w.runtime._read(stage/(stem+name),0,{0o600},limit=131072) for name in names}
    history={'authorization_sha256':wire._digest(authorization_raw),
             'intent_sha256':wire._digest(journals['intent.json']),'done_sha256':wire._digest(journals['done.json']),
             'progress_sha256':{name:wire._digest(journals[name]) for name in names if name.startswith('progress-')}}
    require(CFG['expected'] is None or history['done_sha256']==CFG['expected'])
    require(resume._latest_history(request,raw,hold,active,history)==authorization)
    worker=w.GuestWorker(request)
    postgres,pins,_=worker._prepared_pins(prepared['manifest_sha256'])
    require(pins.installation_id==request.args['installation_id'] and pins.node=='A'
        and pins.attempt_id==request.binding.attempt_id)
    authorities={w.runtime.ROOT/name:pin for name,pin in active['authority_files'].items()}
    require(set(authorities)==set(w.live._authority_paths().values()))
    def inventory():
        require(time.monotonic()<deadline)
        objects=w.restore._observer_inventory(deadline)
        contracts,protected=w._initial_recovery_inventory(request.binding,hold['containers'],
            request.args['protected_inventory_sha256'],None,
            (authorization['container_contracts'],authorization['protected_inventory_sha256']),inventory=lambda:objects)
        require(protected==authorization['protected_inventory_sha256']
            and all(row['running'] is True and row['started_at']!=authorization['container_contracts'][role]['started_at']
                    for role,row in contracts.items()))
        instances={}
        for role,pin in hold['containers'].items():
            item=objects[pin['container_id']];state=item['State']
            require(item['Id']==pin['container_id'] and item['Image']==pin['image_id']
                and type(state['Pid']) is int and state['Pid']>0)
            instances[role]=[pin['container_id'],pin['image_id'],state['Pid'],state['StartedAt']]
        w._assert_car_route_blocked(request,raw,prepared,active)
        require(all(wire._digest(w.runtime._read(path,0,{0o444}))==pin for path,pin in authorities.items()))
        return contracts,instances
    def lease(boundary):
        challenge=wire._digest(wire._bytes({'request':w._scope(request),'history':history,'boundary':boundary}))
        proof=w.live.observe_postboot_a_lease(pins,postgres,recovery_attempt_id=request.args['recovery_attempt_id'],
            postboot=request.args['postboot'],postboot_plan_sha256=request.args['postboot_plan_sha256'],
            challenge_sha256=challenge,deadline=min(deadline,time.monotonic()+.5),minimum_remaining_seconds=1)
        require(wire._hash(proof) and time.monotonic()<deadline)
        return proof
    before=inventory();lease('completion_before')
    context,leaf=w.activation._tls(prepared)
    spec_raw=w.runtime._read(stage/'guest-spec.json',0,{0o400,0o600})
    require(wire._digest(spec_raw)==pins.spec_sha256)
    spec=resume.decode(spec_raw)
    require(spec['node']=='A' and spec['guest_ip']==spec['pg_a'])
    readiness={role:w.activation._probe(context,leaf,spec['guest_ip'],role,deadline,expected_node='A')
               for role in order if role!='ha-broker'}
    require(len(readiness)==7 and all(wire._hash(pin) for pin in readiness.values()))
    require(w.activation._tls(prepared)[1]==leaf)
    require(inventory()==before)
    require(w._read_initial_recovery_hold(request)==(raw,hold,prepared,active)
        and w.runtime._read(stage/'guest-spec.json',0,{0o400,0o600})==spec_raw
        and w.runtime._read(target,0,{0o400},limit=131072)==authorization_raw
        and all(w.runtime._read(stage/(stem+name),0,{0o600},limit=131072)==body for name,body in journals.items()))
    require(resume._latest_history(request,raw,hold,active,history)==authorization)
    final_lease=lease('completion_after')
    require(time.monotonic()<deadline)
    result={'status':'STARTED_VERIFIED','receipt_sha256':history['done_sha256'],
        'authorization_sha256':history['authorization_sha256'],'history':history,
        'request_sha256':wire._digest(wire._bytes(w._scope(request)|{'args':request.args})),
        'service_instance_sha256':CFG['instance'],'instances_sha256':wire._digest(wire._bytes(before[1])),
        'readiness':readiness,'guest_lease_sha256':final_lease,'container_count':len(order),'mutations':False}
print(json.dumps(result,sort_keys=True,separators=(',',':')))
'''


def guest_completion(dispatcher, request, launcher, manifest_sha256, authorization, deadline, *,
                     assert_guard, prior_resume=None, expected_receipt_sha256=None):
    """Observe immutable completion and native mTLS readiness; never dispatch a verb."""
    need(type(dispatcher) is ssh.GuestSshDispatcher and dispatcher.binding == request.binding
         and type(launcher) is bytes and wire._digest(launcher) == dispatcher.launcher_sha256
         and wire._hash(manifest_sha256) and callable(assert_guard))
    marker = b'from ha_control.native_guest_worker import main\nraise SystemExit(main())'
    need(launcher.count(marker) == 1 and manifest_sha256.encode() in launcher)
    data = {'binding': request.binding.fields(), 'node': request.node, 'verb': request.verb,
            'sequence': request.sequence, 'nonce': request.nonce, 'args': request.args,
            'instance': authorization['service_instance_sha256'], 'authorization': authorization,
            'prior_resume': prior_resume, 'expected': expected_receipt_sha256,
            'budget': min(120, wire._remaining(deadline))}
    code = 'import json\nCFG=' + repr(data) + '\n' + launcher.split(marker)[0].decode() + _COMPLETION_READ
    need(len(code.encode()) <= 131072)
    compile(code, '<successor-app-completion>', 'exec')
    assert_guard()
    stamps, records = {}, {}
    for role, endpoint in (('jump', dispatcher.jump), ('A', dispatcher.guests['A'])):
        stamps.update(ssh._identity(endpoint, deadline))
        path = dispatcher.known_hosts[role]
        raw, stamps[path] = ssh._checked_file(path, private=False, maximum=1048576)
        records[endpoint.alias] = ssh._host_record(raw, endpoint, guest=role == 'A')
    root = '/private/tmp' if sys.platform == 'darwin' else '/tmp'
    with tempfile.TemporaryDirectory(prefix='ha-successor-completion-', dir=root) as directory:
        config = ssh._ssh_config(Path(directory), dispatcher.jump, dispatcher.guests['A'], records)
        command = (ssh.SSH, '-F', str(config), '-T', dispatcher.guests['A'].alias,
                   shlex.join(('/usr/bin/python3', '-I', '-B', '-S', '-c', code)))
        need(ssh._same_files(stamps))
        result = json.loads(ssh._exchange(command, b'', deadline), object_pairs_hook=wire._pairs)
        need(ssh._same_files(stamps))
    assert_guard()
    wire._remaining(deadline)
    return result


class SuccessorAppStage:
    """Coordinator adapter. Every issued request consumes this instance once."""

    def __init__(self, *, lease, dispatcher, powershell, proof_inputs, plan, predecessor_terminal,
                 launcher, manifest_sha256, prior_intent_sha256, prior_progress_sha256, plan_raw=None):
        from .native_windows_activation import WindowsActivationLease
        need(type(lease) is WindowsActivationLease and type(dispatcher) is ssh.GuestSshDispatcher)
        proof_inputs.validate()
        plan_raw = wire._bytes(plan) if plan_raw is None else plan_raw
        need(type(plan) is dict and type(plan_raw) is bytes and wire._digest(plan_raw) == proof_inputs.plan_sha256
             and json.loads(plan_raw, object_pairs_hook=wire._pairs) == plan
             and type(predecessor_terminal) is bytes and wire._digest(predecessor_terminal) == proof_inputs.terminal_sha256)
        postboot = scope(plan)
        need(published_postboot(postboot) and TRANSITION_FIELDS <= postboot.keys()
             and wire._hash(postboot.get('carryover_receipt_sha256'))
             and postboot['predecessor_terminal_sha256'] == proof_inputs.terminal_sha256
             and plan['source_commit'] == proof_inputs.source_commit
             and dispatcher.binding == lease.custody.binding
             and plan['initial_cutover_handoff']['attempt_id'] == dispatcher.binding.attempt_id
             and plan['initial_cutover_handoff']['legacy_plan_sha256'] == dispatcher.binding.plan_sha256
             and wire._hash(prior_intent_sha256) and callable(powershell)
             and type(launcher) is bytes and wire._digest(launcher) == dispatcher.launcher_sha256
             and wire._hash(manifest_sha256) and wire._hash(lease.service_instance_sha256))
        self.closed_revision, self.open_revision, self.revoked_revision = derive_predecessor(
            predecessor_terminal, proof_inputs.terminal_sha256, published=True)
        self.lease, self.dispatcher, self.powershell, self.proof_inputs = lease, dispatcher, powershell, proof_inputs
        self.plan = json.loads(wire._bytes(plan))
        self.postboot, self.terminal = postboot, predecessor_terminal
        self.launcher, self.manifest, self.prior_intent = launcher, manifest_sha256, prior_intent_sha256
        need(type(prior_progress_sha256) is dict
             and all(type(key) is str and wire._hash(pin) for key, pin in prior_progress_sha256.items()))
        self.prior_progress = prior_progress_sha256.copy()
        self.consumed = False

    def _proof(self, deadline, guard):
        boundary = PairedAuthorityBoundary(self.powershell, self.proof_inputs, deadline)
        return self.lease.verify_recovery_continuity(
            boundary.powershell, self.plan['initial_cutover_recovery'], deadline,
            observe_pair=boundary.observe_pair, assert_guard=guard, postboot=self.postboot,
            predecessor_terminal=self.terminal)

    def preflight(self, deadline, *, assert_guard):
        """Read-only proof; no authorization is published and no app is started."""
        need(not self.consumed and callable(assert_guard))
        return self._proof(min(deadline, time.monotonic() + wire.ACTIVATION_BOUNDARY_SECONDS), assert_guard)

    def make_request(self, operation_nonce, carryover_raw, *, sequence=1):
        """Project exact protected recovery arguments; never manufacture a baseline."""
        from .native_guest_postboot import CARRYOVER_FIELDS, CARRYOVER_KIND
        need(not self.consumed and wire._hash(operation_nonce) and type(carryover_raw) is bytes
             and wire._digest(carryover_raw) == self.postboot['carryover_receipt_sha256']
             and type(sequence) is int and 0 < sequence <= wire.MAX_REQUESTS)
        carryover = json.loads(carryover_raw, object_pairs_hook=wire._pairs)
        postboot_projection = {key: value for key, value in self.postboot.items()
                               if key not in {'carryover_receipt_sha256', 'predecessor_seal_sha256'}}
        need(type(carryover) is dict and set(carryover) == CARRYOVER_FIELDS
             and carryover['schema_version'] == 1 and type(carryover['schema_version']) is int
             and carryover['kind'] == CARRYOVER_KIND and carryover['binding'] == self.dispatcher.binding.fields()
             and carryover['postboot'] == postboot_projection
             and carryover['history']['intent_sha256'] == self.prior_intent
             and carryover['history']['progress_sha256'] == self.prior_progress
             and carryover['predecessor_request']['scope']['nonce'] != operation_nonce
             and type(carryover['recovery_args']) is dict
             and set(carryover['recovery_args']) == {*wire.INITIAL_RECOVERY_ARGS, 'hold_sha256'})
        args = carryover['recovery_args'].copy() | {
            'postboot': self.postboot.copy(), 'postboot_plan_sha256': self.proof_inputs.plan_sha256,
            'postboot_stopped_receipt_sha256': self.postboot['stopped_receipt_sha256']}
        wire._args(wire.GUEST_START_INITIAL_POSTBOOT, 'A', args)
        need(args['hold_sha256'] == carryover['hold_sha256'] == self.plan['windows_recovery_observers']['hold_sha256']
             and args['installation_id'] == self.plan['installation_id']
             and args['recovery_attempt_id'] == self.plan['initial_cutover_recovery']['attempt_id']
             and args['protected_inventory_sha256'] == carryover['protected_inventory_sha256'])
        return wire.Request(self.dispatcher.binding, 'A', wire.GUEST_START_INITIAL_POSTBOOT,
                            sequence, operation_nonce, args)

    def observe_publication(self, deadline, *, assert_guard, predecessor_binding):
        """Publish proof facts after native current authority and old PID/SCM checks."""
        need(type(predecessor_binding) is bytes
             and wire._digest(predecessor_binding) == self.postboot['predecessor_binding_sha256'])
        old = json.loads(predecessor_binding, object_pairs_hook=wire._pairs)
        expected = {'attempt_id', 'controller_plan_sha256', 'first_write_proof_sha256', 'legacy_plan_sha256',
                    'service_created_ticks', 'service_image_path_sha256', 'service_name', 'service_pid', 'state_path'}
        need(type(old) is dict and set(old) == expected
             and old['attempt_id'] == self.dispatcher.binding.attempt_id
             and old['legacy_plan_sha256'] == self.dispatcher.binding.plan_sha256
             and old['controller_plan_sha256'] == self.postboot['predecessor_plan_sha256']
             and old['service_name'] == self.plan['windows_enforced']['service_name']
             and type(old['service_pid']) is int and old['service_pid'] > 0
             and type(old['service_created_ticks']) is str and re.fullmatch('[1-9][0-9]{0,19}', old['service_created_ticks'])
             and wire._hash(old['service_image_path_sha256'])
             and old['service_image_path_sha256'] != self.postboot['successor_service_image_path_sha256'])
        self.preflight(deadline, assert_guard=assert_guard)
        config = {'old': old, 'binding_path': self.plan['initial_service_start']['binding_path'],
                  'instance': self.lease.service_instance_sha256,
                  'path_pin': self.postboot['successor_service_image_path_sha256']}
        encoded = base64.b64encode(wire._bytes(config)).decode()
        source = r"""
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$utf=[Text.UTF8Encoding]::new($false,$true)
$cfg=ConvertFrom-Json ($utf.GetString([Convert]::FromBase64String('__CONFIG__')))
function Digest([string]$value){
 $sha=[Security.Cryptography.SHA256]::Create()
 try{return [BitConverter]::ToString($sha.ComputeHash($utf.GetBytes($value))).Replace('-','').ToLowerInvariant()}
 finally{$sha.Dispose()}
}
$bindingRaw=[IO.File]::ReadAllText($cfg.binding_path,$utf)
if((Digest $bindingRaw) -cne $cfg.instance){throw 'successor_current_binding'}
$current=ConvertFrom-Json $bindingRaw
$service=Get-CimInstance Win32_Service -Filter ("Name='"+$cfg.old.service_name+"'") -OperationTimeoutSec 5
if($null -eq $service -or $service.State -cne 'Running' -or $service.ProcessId -ne $current.service_pid -or
 $current.service_name -cne $cfg.old.service_name -or $service.StartMode -cne 'Manual' -or
 $service.StartName -ine ('NT SERVICE\'+$cfg.old.service_name) -or
 (Digest $service.PathName) -cne $cfg.path_pin -or (Digest $service.PathName) -ceq $cfg.old.service_image_path_sha256){
 throw 'successor_previous_instance_unconfirmed'
}
$previous=Get-Process -Id $cfg.old.service_pid -ErrorAction SilentlyContinue
if($null -ne $previous){
 try{if(-not $previous.HasExited -and $previous.StartTime.ToUniversalTime().Ticks.ToString() -ceq $cfg.old.service_created_ticks){
   throw 'successor_previous_instance_running'
 }}finally{$previous.Dispose()}
}
if((Digest ([IO.File]::ReadAllText($cfg.binding_path,$utf))) -cne $cfg.instance){throw 'successor_current_binding'}
@{predecessor_instance_stopped=$true}|ConvertTo-Json -Compress
""".replace('__CONFIG__', encoded)
        assert_guard()
        wire._remaining(deadline)
        result = self.powershell(source, deadline)
        need(type(result) is dict and set(result) == {'predecessor_instance_stopped'}
             and result['predecessor_instance_stopped'] is True)
        self.preflight(deadline, assert_guard=assert_guard)
        return {'plan_sha256': self.proof_inputs.plan_sha256, 'revision': self.open_revision,
                'writers': ['A'], 'standby_node': 'B', 'synchronous': True,
                'instance_sha256': self.lease.service_instance_sha256, 'predecessor_instance_stopped': True}

    def start(self, request, deadline, *, assert_guard, issued, record, prior_resume=None):
        """Journal before attestation/dispatch; never automatically repeat a start."""
        need(not self.consumed and type(request) is wire.Request and request.binding == self.dispatcher.binding
             and request.node == 'A' and request.verb == wire.GUEST_START_INITIAL_POSTBOOT
             and callable(assert_guard) and callable(issued) and callable(record))
        wire._args(request.verb, request.node, request.args)
        need(request.args['postboot'] == self.postboot and request.args['postboot_plan_sha256'] == self.proof_inputs.plan_sha256
             and request.args['postboot_stopped_receipt_sha256'] == self.postboot['stopped_receipt_sha256']
             and request.args['recovery_attempt_id'] == self.plan['initial_cutover_recovery']['attempt_id']
             and request.args['installation_id'] == self.plan['installation_id']
             and request.args['hold_sha256'] == self.plan['windows_recovery_observers']['hold_sha256'])
        request = wire.Request(request.binding, request.node, request.verb, request.sequence, request.nonce,
                               json.loads(wire._bytes(request.args)))
        prior_resume = _snapshot_prior_resume(prior_resume)
        assert_guard()
        self.preflight(deadline, assert_guard=assert_guard)
        self.consumed = True
        issued(request)
        authorization = guest_attest(self.dispatcher, request, self.launcher, self.manifest,
                                     self.lease.service_instance_sha256, self.prior_intent, self.prior_progress,
                                     deadline, assert_guard=assert_guard,
                                     **({} if prior_resume is None else {'prior_resume': prior_resume}))
        record('authorization', authorization)
        request_scope = {'version': wire.VERSION, 'binding': request.binding.fields(), 'node': request.node,
                         'boot_sha256': request.binding.boot(request.node), 'verb': request.verb,
                         'sequence': request.sequence, 'nonce': request.nonce}

        def current(challenge, boundary, limit):
            need(boundary in wire.RECOVERY_START_BOUNDARIES and wire._hash(challenge))
            expected = wire._digest(wire._bytes({'request': request_scope, 'hold_sha256': request.args['hold_sha256'],
                        'boundary': boundary, 'postboot': self.postboot, 'postboot_plan_sha256': self.proof_inputs.plan_sha256,
                        'resume_authorization_sha256': authorization['authorization_sha256']}))
            need(challenge == expected)
            return self._proof(min(limit, deadline), assert_guard)

        receipt = self.dispatcher.start_initial_postboot(request, deadline, assert_windows_current=current)
        need(type(receipt) is wire.Receipt and receipt.status == 'PASS' and wire._hash(receipt.receipt_sha256))
        assert_guard()
        record('started', {'receipt_sha256': receipt.receipt_sha256,
                           'authorization_sha256': authorization['authorization_sha256'],
                           'closed_revision': self.closed_revision, 'open_revision': self.open_revision})
        return receipt.receipt_sha256

    def reconcile(self, request, authorization, deadline, *, assert_guard, prior_resume=None,
                  expected_receipt_sha256=None):
        """Read completion of an already issued request, including after lost replies.

        The caller supplies its durable issued request and authorization receipt.
        Both consumed and reconstructed stage instances are accepted. Missing or
        partial completion raises instead of starting, attesting, or retrying.
        Only local dispatch eligibility is consumed; no guest record is written.
        """
        need(type(request) is wire.Request and request.binding == self.dispatcher.binding
             and request.node == 'A' and request.verb == wire.GUEST_START_INITIAL_POSTBOOT
             and callable(assert_guard))
        wire._args(request.verb, request.node, request.args)
        need(request.args['postboot'] == self.postboot
             and request.args['postboot_plan_sha256'] == self.proof_inputs.plan_sha256
             and request.args['postboot_stopped_receipt_sha256'] == self.postboot['stopped_receipt_sha256']
             and request.args['recovery_attempt_id'] == self.plan['initial_cutover_recovery']['attempt_id']
             and request.args['installation_id'] == self.plan['installation_id']
             and request.args['hold_sha256'] == self.plan['windows_recovery_observers']['hold_sha256']
             and type(request.sequence) is int and 0 < request.sequence <= wire.MAX_REQUESTS
             and wire._hash(request.nonce)
             and type(authorization) is dict and authorization.get('request_nonce') == request.nonce
             and authorization.get('service_instance_sha256') == self.lease.service_instance_sha256
             and authorization.get('prior_intent_sha256') == self.prior_intent
             and authorization.get('prior_progress_sha256') == self.prior_progress
             and wire._hash(authorization.get('authorization_sha256'))
             and (expected_receipt_sha256 is None or wire._hash(expected_receipt_sha256)))
        request = wire.Request(request.binding, request.node, request.verb, request.sequence, request.nonce,
                               json.loads(wire._bytes(request.args)))
        authorization = json.loads(wire._bytes(authorization))
        prior_resume = _snapshot_prior_resume(prior_resume)
        self.consumed = True
        assert_guard()
        before = self._proof(min(deadline, time.monotonic() + wire.ACTIVATION_BOUNDARY_SECONDS), assert_guard)
        result = guest_completion(self.dispatcher, request, self.launcher, self.manifest, authorization, deadline,
                                  assert_guard=assert_guard, prior_resume=prior_resume,
                                  expected_receipt_sha256=expected_receipt_sha256)
        assert_guard()
        after = self._proof(min(deadline, time.monotonic() + wire.ACTIVATION_BOUNDARY_SECONDS), assert_guard)
        request_pin = wire._digest(wire._bytes({'version': wire.VERSION, 'binding': request.binding.fields(),
            'node': request.node, 'boot_sha256': request.binding.boot(request.node), 'verb': request.verb,
            'sequence': request.sequence, 'nonce': request.nonce, 'args': request.args}))
        fields = {'status', 'receipt_sha256', 'authorization_sha256', 'history', 'request_sha256',
                  'service_instance_sha256', 'instances_sha256', 'readiness', 'guest_lease_sha256',
                  'container_count', 'mutations'}
        need(type(result) is dict and set(result) == fields and result['status'] == 'STARTED_VERIFIED'
             and result['request_sha256'] == request_pin
             and result['authorization_sha256'] == authorization['authorization_sha256']
             and result['service_instance_sha256'] == self.lease.service_instance_sha256
             and type(result['container_count']) is int and result['container_count'] == 8
             and result['mutations'] is False and wire._hash(result['receipt_sha256'])
             and wire._hash(result['instances_sha256']) and wire._hash(result['guest_lease_sha256'])
             and type(result['readiness']) is dict
             and set(result['readiness']) == set(wire.RECOVERY_START_ORDER) - {'ha-broker'}
             and all(wire._hash(pin) for pin in result['readiness'].values())
             and type(result['history']) is dict
             and set(result['history']) == {'authorization_sha256', 'intent_sha256', 'progress_sha256', 'done_sha256'}
             and result['history']['authorization_sha256'] == authorization['authorization_sha256']
             and result['history']['done_sha256'] == result['receipt_sha256']
             and wire._hash(result['history']['intent_sha256'])
             and type(result['history']['progress_sha256']) is dict
             and set(result['history']['progress_sha256']) == {'progress-' + role + '.json' for role in wire.RECOVERY_START_ORDER}
             and all(wire._hash(pin) for pin in result['history']['progress_sha256'].values())
             and (expected_receipt_sha256 is None or result['receipt_sha256'] == expected_receipt_sha256)
             and wire._hash(before) and wire._hash(after))
        return result | {'closed_revision': self.closed_revision, 'open_revision': self.open_revision,
                         'windows_proof_before_sha256': before, 'windows_proof_after_sha256': after}
