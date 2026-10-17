#!/usr/bin/env python3
"""Controlled local incident evidence; never opens production URLs or databases."""
import argparse
from datetime import datetime,timezone
import hashlib
import importlib.metadata
import json
import multiprocessing as mp
import os
from pathlib import Path
import platform
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import time

from incident_search import search_actor
from incident_sqlite import sqlite_actor,lock_actor

ROOT=Path(__file__).resolve().parents[2]
SCENARIOS=('dependency_timeout','sqlite_busy','sqlite_full')
EVENTS=['baseline_verified','fault_applied','failure_observed','cause_evidence_verified',
        'recovery_action_started','recovery_action_completed','recovery_verified','cleanup_verified']


class Stopped(Exception):
    pass


def receive(connection,process,deadline,interrupted):
    limit=min(deadline,time.monotonic()+12)
    while True:
        if interrupted():
            raise Stopped('interrupted')
        if time.monotonic()>=limit:
            raise Stopped('deadline' if time.monotonic()>=deadline else 'ipc_timeout')
        if connection.poll(.02):
            try:
                return connection.recv()
            except EOFError:
                raise Stopped('actor_closed') from None
        if not process.is_alive():
            raise Stopped('actor_exited')


def command(connection,process,deadline,interrupted,op,**kwargs):
    connection.send({'op':op,**kwargs})
    response=receive(connection,process,deadline,interrupted)
    if response.get('ok') is not True:
        raise Stopped('actor_command_failed')
    return response.get('data',{})


def cleanup(children):
    for process,connection,op in children:
        try:
            connection.send({'op':op})
        except OSError:
            pass
    end=time.monotonic()+5
    for process,_,_ in children:
        process.join(max(0,end-time.monotonic()))
    for process,_,_ in children:
        if process.is_alive():
            process.terminate()
    end=time.monotonic()+5
    for process,_,_ in children:
        process.join(min(.5,max(0,end-time.monotonic())))
    for process,_,_ in children:
        if process.is_alive():
            process.kill()
    for process,connection,_ in children:
        process.join(max(0,end-time.monotonic()))
        connection.close()
    return all(not p.is_alive() for p,_,_ in children)


def run_scenario(scenario,deadline,interrupted):
    import tempfile
    started=time.monotonic()
    result={'scenario':scenario,'valid':False,'reason':'initialization','events':[],
            'observations':{},'cleanup_ok':False,'fixture_removed':False,'timings':{}}
    children=[]; sockets=[]; root=None

    def event(name):
        result['events'].append({'event':name,'elapsed_seconds':time.monotonic()-started,
                                 'at':datetime.now(timezone.utc).isoformat()})

    def spawn(target,args,stop='stop'):
        parent,child=mp.get_context('spawn').Pipe()
        process=mp.get_context('spawn').Process(target=target,args=(child,*args))
        try:
            process.start()
        except Exception:
            parent.close(); child.close()
            raise
        child.close(); children.append((process,parent,stop))
        if receive(parent,process,deadline,interrupted)!={'ready':True}:
            raise Stopped('actor_not_ready')
        return process,parent

    try:
        if interrupted() or time.monotonic()>=deadline:
            raise Stopped('interrupted' if interrupted() else 'deadline')
        root=Path(tempfile.mkdtemp(prefix='incident-fixture-'))
        nonce=secrets.token_hex(16)
        (root/'.incident-owner').write_text(nonce)
        if scenario=='dependency_timeout':
            for _ in range(3):
                sock=socket.socket(); sock.bind(('127.0.0.1',0)); sock.listen(128)
                sockets.append(sock)
            process,connection=spawn(search_actor,(sockets,nonce))
            for sock in sockets:
                sock.close()
        else:
            process,connection=spawn(sqlite_actor,(str(root),))

        def call(op,**kwargs):
            return command(connection,process,deadline,interrupted,op,**kwargs)

        baseline=call('probe' if scenario=='dependency_timeout' else 'baseline')
        if scenario=='dependency_timeout' and not (baseline.get('valid') and baseline.get('statuses')==['ok']*3):
            raise Stopped('baseline_invalid')
        result['observations']['baseline']=baseline
        event('baseline_verified')
        if scenario=='dependency_timeout':
            call('fault')
        elif scenario=='sqlite_busy':
            locker,lock_connection=spawn(lock_actor,(str(root),),stop='release')
        else:
            result['observations']['capacity']=call('constrain')
        event('fault_applied')
        failure=call('probe',**({} if scenario=='dependency_timeout' else {'scenario':scenario}))
        observed=(failure.get('valid') and failure.get('statuses')==['ok','ok','unavailable']
                  if scenario=='dependency_timeout' else
                  failure.get('error_code')==(5 if scenario=='sqlite_busy' else 13))
        result['observations']['failure']=failure
        if not observed:
            raise Stopped('fault_not_observed')
        event('failure_observed')
        if scenario!='dependency_timeout':
            failure.update(call('evidence'))
            if failure.get('unchanged') is not True:
                raise Stopped('failed_write_changed_data')
        event('cause_evidence_verified')
        event('recovery_action_started')
        if scenario=='dependency_timeout':
            call('normal')
        elif scenario=='sqlite_busy':
            command(lock_connection,locker,deadline,interrupted,'release')
            locker.join(timeout=min(5,max(0,deadline-time.monotonic())))
            if locker.is_alive() or locker.exitcode!=0:
                raise Stopped('locker_not_released')
        else:
            call('release')
        event('recovery_action_completed')
        recovered=call('probe' if scenario=='dependency_timeout' else 'verify')
        valid=(recovered.get('valid') and recovered.get('statuses')==['ok']*3
               if scenario=='dependency_timeout' else
               all(recovered.get(key) is True for key in ['data_preserved','exactly_once','integrity_ok']))
        result['observations']['recovery']=recovered
        if not valid:
            raise Stopped('recovery_invalid')
        event('recovery_verified')
        call('stop')
        result['valid'],result['reason']=True,'ok'
    except Stopped as error:
        result['reason']=str(error)
    except (Exception,KeyboardInterrupt):
        result['reason']='controller_failed'
    finally:
        result['cleanup_ok']=cleanup(children)
        result['children_exited_cleanly']=all(p.exitcode==0 for p,_,_ in children)
        for sock in sockets:
            sock.close()
        if root is not None and result['cleanup_ok']:
            try:
                shutil.rmtree(root)
                result['fixture_removed']=not root.exists()
            except OSError:
                pass
        elif root is None:
            result['fixture_removed']=True
        if result['cleanup_ok'] and result['fixture_removed']:
            event('cleanup_verified')
        else:
            result['valid'],result['reason']=False,'cleanup_failed'
        if result['valid'] and not result['children_exited_cleanly']:
            result['valid'],result['reason']=False,'child_failed'
        times={e['event']:e['elapsed_seconds'] for e in result['events']}
        for key,start,end in [('detection_seconds','fault_applied','failure_observed'),
                              ('automatic_cause_check_seconds','failure_observed','cause_evidence_verified'),
                              ('recovery_check_seconds','recovery_action_started','recovery_verified')]:
            result['timings'][key]=times[end]-times[start] if start in times and end in times else None
    return result


def metadata():
    def git(*args):
        try:
            return subprocess.check_output(['git',*args],cwd=ROOT,stderr=subprocess.DEVNULL,text=True,timeout=5).strip()
        except (OSError,subprocess.SubprocessError):
            return None
    files=[Path(__file__),Path(__file__).with_name('incident_sqlite.py'),Path(__file__).with_name('incident_search.py'),
           ROOT/'book-memo/app/services/book_service.py',ROOT/'portal-web/app/services/global_search.py']
    return {'git_sha':git('rev-parse','HEAD'),'dirty':bool(git('status','--porcelain')),
            'python':platform.python_version(),'sqlite':sqlite3.sqlite_version,
            'httpx':importlib.metadata.version('httpx'),'os':platform.system(),'architecture':platform.machine(),
            'source_hashes':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}


def arguments(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--scenario',choices=SCENARIOS,action='append')
    parser.add_argument('--repeats',type=int,default=1)
    parser.add_argument('--deadline',type=float,default=180)
    args=parser.parse_args(argv)
    args.scenario=args.scenario or list(SCENARIOS)
    if not 1<=args.repeats<=3 or not 0<args.deadline<=180 or len(set(args.scenario))!=len(args.scenario):
        parser.error('repeats=1..3, deadline=0..180 exclusive zero, unique scenarios required')
    return args


def main(argv=None):
    args=arguments(argv); interrupted=False

    def stop(_sig,_frame):
        nonlocal interrupted
        interrupted=True

    previous={sig:signal.signal(sig,stop) for sig in [signal.SIGINT,signal.SIGTERM]}
    report={'schema_version':1,'valid':False,'reason':'initialization','cases':[],
            'scope':'automated_local_known_faults_not_production_MTTR_RTO_RPO',
            'started_at':datetime.now(timezone.utc).isoformat(),
            'remaining_cases':len(args.scenario)*args.repeats,
            'configuration':{'scenarios':args.scenario,'repeats':args.repeats,'deadline_seconds':args.deadline}}
    deadline=time.monotonic()+args.deadline
    try:
        with args.output.open('x',encoding='utf-8') as output:
            def save():
                output.seek(0); json.dump(report,output,ensure_ascii=False,indent=2,allow_nan=False)
                output.truncate(); output.flush()
            save()
            try:
                report['environment']=metadata()
                report['reason']='ok'
                for scenario in args.scenario:
                    for repeat in range(1,args.repeats+1):
                        if interrupted or time.monotonic()>=deadline:
                            raise Stopped('interrupted' if interrupted else 'deadline')
                        case=run_scenario(scenario,deadline,lambda:interrupted)
                        case['repeat']=repeat; report['cases'].append(case); report['remaining_cases']-=1
                        save()
                        if not case['valid']:
                            raise Stopped(case['reason'])
                report['valid']=True
                if interrupted:
                    raise Stopped('interrupted')
            except Stopped as error:
                report['valid'],report['reason']=False,str(error)
            except Exception:
                report['valid'],report['reason']=False,'runner_failed'
            finally:
                report['finished_at']=datetime.now(timezone.utc).isoformat(); save()
    except (OSError,ValueError):
        print('incident_lab=FAIL reason=output_unavailable')
        return 1
    finally:
        for sig,handler in previous.items():
            signal.signal(sig,handler)
    print(f"incident_lab={'PASS' if report['valid'] else 'FAIL'} cases={len(report['cases'])} reason={report['reason']}")
    return 0 if report['valid'] else 1


if __name__=='__main__':
    mp.freeze_support()
    raise SystemExit(main())
