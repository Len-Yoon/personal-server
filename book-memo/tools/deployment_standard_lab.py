#!/usr/bin/env python3
"""Standardized local Book releases; no production, containers or remote targets."""
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
import tempfile
import time

import httpx
from incident_recovery_lab import Stopped,receive,cleanup
from release_actor import release_actor,MEMO,CONTENT
from release_artifact import build_release,verify_release,git_environment

ROOT=Path(__file__).resolve().parents[2]
SCENARIOS=('redeploy','readiness_failure','artifact_tamper')


def check_budget(deadline,interrupted):
    if interrupted() or time.monotonic()>=deadline:
        raise Stopped('interrupted' if interrupted() else 'deadline')


def command(actor,deadline,interrupted,op):
    process,connection,_=actor
    connection.send({'op':op})
    reply=receive(connection,process,deadline,interrupted)
    if not isinstance(reply,dict) or reply.get('ok') is not True:
        raise Stopped('actor_command_failed')
    return reply.get('data',{})


def start_release(root,descriptor,children,deadline,interrupted):
    if interrupted() or time.monotonic()>=deadline:
        raise Stopped('interrupted' if interrupted() else 'deadline')
    if any(process.is_alive() for process,_,_ in children):
        raise Stopped('previous_actor_alive')
    path=root/descriptor['release_id']
    verify_release(path,descriptor,deadline=deadline,interrupted=interrupted)
    sock=socket.socket()
    parent=child=None
    try:
        sock.bind(('127.0.0.1',0));sock.listen(128)
        port=sock.getsockname()[1]
        parent,child=mp.get_context('spawn').Pipe()
        process=mp.get_context('spawn').Process(target=release_actor,
            args=(child,str(path),descriptor,str(root/'fixture.sqlite3'),sock))
        process.start()
        child.close();child=None
        actor=(process,parent,'stop');children.append(actor)
        reply=receive(parent,process,deadline,interrupted)
        expected={'ready':True,'release_digest':descriptor['release_digest'],'pid':process.pid,
                  'owner':(root/'.release-lab-owner').read_text()}
        if reply!=expected:
            raise Stopped('unexpected_actor_identity')
        return actor,port
    except Exception:
        if parent is not None and not any(parent is item[1] for item in children):
            parent.close()
        raise
    finally:
        if child is not None:
            child.close()
        sock.close()


def stop_release(actor,deadline,interrupted):
    command(actor,deadline,interrupted,'stop')
    actor[0].join(min(5,max(0,deadline-time.monotonic())))
    if actor[0].is_alive() or actor[0].exitcode!=0:
        raise Stopped('previous_actor_not_reaped')


def http_read(port,path,deadline,interrupted):
    if interrupted() or time.monotonic()>=deadline:
        raise Stopped('interrupted' if interrupted() else 'deadline')
    limit=min(deadline,time.monotonic()+5)
    content=bytearray()
    try:
        with httpx.Client(trust_env=False,follow_redirects=False,timeout=min(.5,max(.01,limit-time.monotonic()))) as client:
            with client.stream('GET',f'http://127.0.0.1:{port}{path}') as response:
                for chunk in response.iter_bytes():
                    if interrupted() or time.monotonic()>=limit:
                        raise Stopped('interrupted' if interrupted() else 'http_deadline')
                    content.extend(chunk)
                    if len(content)>128*1024:
                        raise Stopped('http_body_limit')
                return response.status_code,bytes(content)
    except httpx.HTTPError:
        raise Stopped('http_probe_failed') from None


def probe(port,descriptor,deadline,interrupted):
    health,body=http_read(port,'/health',deadline,interrupted)
    readiness,ready_body=http_read(port,descriptor['readiness_path'],deadline,interrupted)
    try:
        valid=health==200 and json.loads(body)=={'service':'book-memo','status':'ok'}
        valid=valid and readiness==200 and json.loads(ready_body)=={'service':'book-memo','status':'ready'}
    except (ValueError,UnicodeError):
        valid=False
    return {'health_status':health,'readiness_status':readiness,'valid':valid}


def preserved(actor,port,baseline,deadline,interrupted):
    snapshot=command(actor,deadline,interrupted,'snapshot')
    code,body=http_read(port,'/books/1',deadline,interrupted)
    functional=code==200 and MEMO.encode() in body and CONTENT.encode() in body
    valid=snapshot==baseline and snapshot.get('integrity_ok') is True and snapshot.get('fixture_valid') is True
    return valid,functional


def run_scenario(scenario,deadline,interrupted):
    result={'scenario':scenario,'valid':False,'reason':'initialization','events':[],
            'releases':{},'observations':{},'deployment_succeeded':False,
            'rollback_succeeded':None,'rollback_attempts':0,'starts':0,'active_release':None,
            'data_preserved':False,'functional_read_ok':False,'cleanup_ok':False,
            'fixture_removed':False,'children_exited_cleanly':False}
    children=[];root=None;started=time.monotonic()
    def event(name):
        result['events'].append({'event':name,'elapsed_seconds':time.monotonic()-started,
                                'at':datetime.now(timezone.utc).isoformat()})
    def activate(descriptor):
        actor,port=start_release(root,descriptor,children,deadline,interrupted)
        result['starts']+=1
        return actor,port
    try:
        if interrupted() or time.monotonic()>=deadline:
            raise Stopped('interrupted' if interrupted() else 'deadline')
        root=Path(tempfile.mkdtemp(prefix='release-fixture-')).resolve()
        (root/'.release-lab-owner').write_text(secrets.token_hex(16))
        check_budget(deadline,interrupted)
        commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True,
                                      env=git_environment(),
                                      timeout=max(.001,min(5,deadline-time.monotonic()))).strip()
        baseline=build_release(root/'baseline',commit,'baseline','/ready',deadline=deadline,interrupted=interrupted)
        candidate_id='unready' if scenario=='readiness_failure' else 'candidate'
        candidate=build_release(root/candidate_id,commit,candidate_id,
                    '/__lab_missing_readiness' if scenario=='readiness_failure' else '/ready',
                    deadline=deadline,interrupted=interrupted)
        result['releases']={'baseline':baseline,'candidate':candidate}
        event('releases_verified')
        actor,port=activate(baseline)
        observed=probe(port,baseline,deadline,interrupted)
        if not observed['valid']:
            raise Stopped('baseline_not_ready')
        seed=command(actor,deadline,interrupted,'seed')
        if not (seed.get('integrity_ok') and seed.get('fixture_valid')):
            raise Stopped('seed_invalid')
        result['observations']['baseline_snapshot']=seed
        result['observations']['baseline_probe']=observed
        result['active_release']='baseline'
        event('baseline_verified')
        if scenario=='artifact_tamper':
            (root/candidate_id/'app/main.py').write_bytes(b'# deliberate local artifact corruption\n')
            try:
                verify_release(root/candidate_id,candidate,deadline=deadline,interrupted=interrupted)
            except ValueError:
                result['observations']['preflight_rejected']=True
                event('candidate_preflight_rejected')
            else:
                raise Stopped('tamper_not_detected')
        else:
            verify_release(root/candidate_id,candidate,deadline=deadline,interrupted=interrupted)
            event('candidate_preflight_verified')
            stop_release(actor,deadline,interrupted)
            result['active_release']=None
            event('previous_stopped')
            actor,port=activate(candidate)
            event('candidate_started')
            observed=probe(port,candidate,deadline,interrupted)
            result['observations']['candidate_probe']=observed
            if scenario=='readiness_failure':
                if observed['health_status']!=200 or observed['readiness_status']!=404:
                    raise Stopped('expected_readiness_failure_missing')
                event('candidate_readiness_rejected')
                stop_release(actor,deadline,interrupted)
                event('candidate_stopped')
                result['rollback_attempts']=1
                result['rollback_succeeded']=False
                event('rollback_started')
                actor,port=activate(baseline)
                restored=probe(port,baseline,deadline,interrupted)
                result['observations']['rollback_probe']=restored
                if not restored['valid']:
                    raise Stopped('rollback_not_ready')
                result['active_release']='baseline'
                event('rollback_ready')
            elif not observed['valid']:
                raise Stopped('candidate_not_ready')
            else:
                result['active_release']='candidate'
                event('candidate_ready')
        result['data_preserved'],result['functional_read_ok']=preserved(actor,port,seed,deadline,interrupted)
        if not (result['data_preserved'] and result['functional_read_ok']):
            raise Stopped('data_verification_failed')
        event('data_verified')
        if scenario=='redeploy':
            result['deployment_succeeded']=True
        elif scenario=='readiness_failure':
            result['rollback_succeeded']=True
            event('rollback_verified')
        stop_release(actor,deadline,interrupted)
        event('final_actor_stopped')
        result['valid'],result['reason']=True,'ok'
    except Stopped as error:
        result['reason']=str(error)
    except (Exception,KeyboardInterrupt):
        result['reason']=('interrupted' if interrupted() else
                          'deadline' if time.monotonic()>=deadline else 'controller_failed')
    finally:
        result['cleanup_ok']=cleanup(children)
        result['children_exited_cleanly']=all(p.exitcode==0 for p,_,_ in children)
        if root is not None and result['cleanup_ok']:
            try:
                shutil.rmtree(root);result['fixture_removed']=not root.exists()
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
        if not result['valid']:
            result['deployment_succeeded']=False
        result['timings']={'total_seconds':time.monotonic()-started}
    return result


def metadata(deadline,interrupted):
    def git(*args):
        check_budget(deadline,interrupted)
        return subprocess.check_output(['git',*args],cwd=ROOT,text=True,
                                      env=git_environment(),
                                      timeout=max(.001,min(5,deadline-time.monotonic()))).strip()
    files=['book-memo/tools/deployment_standard_lab.py','book-memo/tools/release_actor.py',
           'book-memo/tools/release_artifact.py','book-memo/tools/incident_recovery_lab.py']
    return {'git_sha':git('rev-parse','HEAD'),'dirty':bool(git('status','--porcelain')),
            'python':platform.python_version(),'sqlite':sqlite3.sqlite_version,
            'os':platform.system(),'architecture':platform.machine(),
            'packages':{name:importlib.metadata.version(name) for name in ['fastapi','uvicorn','httpx']},
            'source_hashes':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in files}}


def arguments(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--scenario',choices=SCENARIOS,action='append')
    parser.add_argument('--repeats',type=int,default=1)
    parser.add_argument('--deadline',type=float,default=180)
    args=parser.parse_args(argv);args.scenario=args.scenario or list(SCENARIOS)
    if not 1<=args.repeats<=3 or not 0<args.deadline<=180 or len(set(args.scenario))!=len(args.scenario):
        parser.error('repeats=1..3, deadline=0..180 exclusive zero, unique scenarios required')
    return args


def main(argv=None):
    args=arguments(argv);interrupted=False
    def stop(_sig,_frame):
        nonlocal interrupted
        interrupted=True
    previous={sig:signal.signal(sig,stop) for sig in [signal.SIGINT,signal.SIGTERM]}
    report={'schema_version':1,'valid':False,'reason':'initialization','cases':[],
            'scope':'same_source_local_process_redeployment_not_production_or_container_rollout',
            'started_at':datetime.now(timezone.utc).isoformat(),
            'remaining_cases':len(args.scenario)*args.repeats,
            'configuration':{'scenarios':args.scenario,'repeats':args.repeats,'deadline_seconds':args.deadline}}
    deadline=time.monotonic()+args.deadline
    try:
        with args.output.open('x',encoding='utf-8') as output:
            def save():
                output.seek(0);json.dump(report,output,ensure_ascii=False,indent=2,allow_nan=False)
                output.truncate();output.flush()
            save()
            try:
                report['environment']=metadata(deadline,lambda:interrupted)
                for scenario in args.scenario:
                    for repeat in range(1,args.repeats+1):
                        if interrupted or time.monotonic()>=deadline:
                            raise Stopped('interrupted' if interrupted else 'deadline')
                        case=run_scenario(scenario,deadline,lambda:interrupted)
                        case['repeat']=repeat;report['cases'].append(case);report['remaining_cases']-=1
                        save()
                        if not case['valid']:
                            raise Stopped(case['reason'])
                report['valid'],report['reason']=True,'ok'
                if interrupted:
                    raise Stopped('interrupted')
            except Stopped as error:
                report['valid'],report['reason']=False,str(error)
            except Exception:
                report['valid']=False
                report['reason']=('interrupted' if interrupted else
                                  'deadline' if time.monotonic()>=deadline else 'runner_failed')
            finally:
                report['finished_at']=datetime.now(timezone.utc).isoformat();save()
    except (OSError,ValueError):
        print('deployment_lab=FAIL reason=output_unavailable');return 1
    finally:
        for sig,handler in previous.items():
            signal.signal(sig,handler)
    print(f"deployment_lab={'PASS' if report['valid'] else 'FAIL'} cases={len(report['cases'])} reason={report['reason']}")
    return 0 if report['valid'] else 1


if __name__=='__main__':
    mp.freeze_support()
    raise SystemExit(main())
