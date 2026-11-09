"""Entrypoint child gate doubles; Linux containment acceptance is separate."""
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'infra/k8s'))
from ha_egress import app_entrypoint as entry
from ha_runtime import config


class EntrypointTests(unittest.TestCase):
    def test_hosts_requires_exact_single_mapping_for_all_three_names(self):
        raw=b'127.0.0.1 localhost\n127.0.0.2 ha-a\n127.0.0.3 ha-b\n127.0.0.4 ha-broker\n'
        self.assertEqual(entry.parse_hosts(raw),{name:[ip] for name,ip in entry.ALIASES.items()})
        for value in (raw+b'10.0.0.1 ha-a\n',raw.replace(b'127.0.0.4',b'127.0.0.1'),raw.replace(b'ha-broker',b'other')):
            with self.assertRaises(entry.NetworkUnavailable):entry.parse_hosts(value)

    def test_invalid_approval_or_capability_preflight_always_has_zero_children(self):
        command=['uvicorn','app.main:app','--host','0.0.0.0','--port','8003']
        with patch.object(entry,'validate_application',side_effect=entry.NetworkUnavailable()),patch.object(entry.subprocess,'Popen') as launch,patch.object(entry,'prepare_forwarder') as prepare:
            with self.assertRaises(entry.NetworkUnavailable):entry.run_application('book-memo',8003,command)
            prepare.assert_not_called();launch.assert_not_called()
        with patch.object(entry,'validate_application'),patch.object(entry,'prepare_forwarder',side_effect=entry.NetworkUnavailable()),patch.object(entry.subprocess,'Popen') as launch:
            with self.assertRaises(entry.NetworkUnavailable):entry.run_application('book-memo',8003,command)
            launch.assert_not_called()

    def test_original_argv_is_preserved_shell_is_disabled_and_streams_close(self):
        forwarder=MagicMock();child=MagicMock(pid=999999);child.poll.return_value=0
        command=['python','-m','app.main','literal $value; not-a-shell']
        with patch.object(entry,'validate_application') as validate,patch.object(entry,'prepare_forwarder',return_value=(forwarder,[],[])),patch.object(entry.subprocess,'Popen',return_value=child) as launch,patch.object(entry.os,'killpg',side_effect=lambda pid,sig: (_ for _ in ()).throw(ProcessLookupError()) if sig==0 else None) as kill:
            self.assertEqual(entry.run_application('car-care-worker',8015,command),0)
            validate.assert_called_once_with('car-care-worker')
            launch.assert_called_once_with(command,shell=False,close_fds=True,start_new_session=True)
            forwarder.start.assert_called_once();forwarder.close.assert_called_once()
            self.assertEqual(kill.call_args_list[0].args,(child.pid,entry.signal.SIGTERM))

    def test_spawn_failure_still_closes_all_streams(self):
        forwarder=MagicMock()
        with patch.object(entry,'validate_application'),patch.object(entry,'prepare_forwarder',return_value=(forwarder,[],[])),patch.object(entry.subprocess,'Popen',side_effect=OSError('private path')):
            with self.assertRaises(OSError):entry.run_application('book-memo',8003,['python'])
            forwarder.close.assert_called_once()

    def test_cli_requires_delimiter_and_cannot_override_capability_addresses(self):
        for argv in (['--service','book-memo','--app-port','8003','uvicorn'],
                     ['--service','book-memo','--host','other','--','uvicorn'],
                     ['--service','book-memo','--app-port','5432','--','uvicorn']):
            with patch.object(entry,'validate_application'),patch.object(entry.subprocess,'Popen') as launch,patch('sys.stderr',io.StringIO()):
                self.assertEqual(entry.main(argv),78);launch.assert_not_called()

    def test_signed_loader_rejection_never_binds_a_listener_or_launches_child(self):
        status='NoNewPrivs:\t1\n'+''.join(key+':\t0000000000000000\n' for key in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb'))
        with patch.object(entry.sys,'platform','linux'),patch.object(entry.os,'geteuid',return_value=10001),patch.object(entry.os,'getegid',return_value=10001),patch.object(entry.os,'getgroups',return_value=[10004]),patch.object(entry.socket,'if_nameindex',return_value=[(1,'lo')]),patch.object(entry.os,'statvfs',return_value=SimpleNamespace(f_flag=os.ST_RDONLY)),patch.object(entry.Path,'read_text',return_value=status),patch.dict(os.environ,{'APP_ENV':'production'}),patch.object(config,'load',side_effect=config.DatabaseUnavailable('invalid authority')),patch.object(entry.socket,'socket') as bind,patch.object(entry.subprocess,'Popen') as child:
            with self.assertRaises(entry.NetworkUnavailable):entry.run_application('book-memo',8003,['uvicorn'])
            bind.assert_not_called();child.assert_not_called()

    def test_socket_replacement_before_connect_is_rejected_without_network(self):
        captured=(1,2,10003,10001,0o660,3)
        with patch.object(entry,'socket_stamp',return_value=captured):capability=entry.UnixCapability('/run/ha-network/pg-a.sock',('directory',))
        descriptor=os.open(os.devnull,os.O_RDONLY)
        with patch.object(entry,'secure_directory',return_value=(descriptor,('directory',))),patch.object(entry,'socket_stamp',return_value=(1,999,10003,10001,0o660,4)),patch.object(entry.socket,'socket') as connect:
            with self.assertRaises(entry.NetworkUnavailable):capability.connect()
            connect.assert_not_called()


    def test_exited_parent_does_not_leave_sigterm_ignoring_group_helpers(self):
        child=MagicMock(pid=999999);child.poll.return_value=0
        with patch.object(entry.os,'killpg') as kill,patch.object(entry.time,'monotonic',side_effect=[0.,4.]):
            entry._stop_child(child)
        self.assertEqual([call.args for call in kill.call_args_list],[(child.pid,entry.signal.SIGTERM),(child.pid,entry.signal.SIGKILL)])
        child.wait.assert_any_call(timeout=1)


if __name__=='__main__':unittest.main()
