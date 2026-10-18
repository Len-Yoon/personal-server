#!/usr/bin/env python3
"""Actual two-version Book container lab, restricted to owned local resources."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import secrets
import shutil
import signal
import stat
import tempfile
import time

from container_runtime import LocalDocker
from release_artifact import _git_bytes,_git_archive,_unpack,_canonical

ROOT=Path(__file__).resolve().parents[2]
OLD_COMMIT='d0a60639ccef6d8acdf821528baf88a1bb6af578'
SCENARIOS=('upgrade','roundtrip','readiness_failure')
SHA=re.compile(r'[0-9a-f]{64}\Z')
COMMIT=re.compile(r'[0-9a-f]{40}\Z')


class Stopped(Exception):
    pass


def budget(deadline,interrupted):
    if interrupted() or time.monotonic()>=deadline:
        raise Stopped('interrupted' if interrupted() else 'deadline')


def prepare_context(destination,app_commit,build_commit,deadline,interrupted):
    try:
        budget(deadline,interrupted)
        if (not isinstance(app_commit,str) or not COMMIT.fullmatch(app_commit)
                or not isinstance(build_commit,str) or not COMMIT.fullmatch(build_commit)
                or app_commit not in {OLD_COMMIT,build_commit}):
            raise ValueError('invalid_context')
        destination=Path(destination).absolute()
        if destination.name not in ('v1','v2') or destination.parent.resolve()!=destination.parent:
            raise ValueError('invalid_context')
        marker=destination.parent/'.container-lab-owner';info=marker.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size!=32
                or not re.fullmatch('[0-9a-f]{32}',marker.read_text()) or destination.exists() or destination.is_symlink()):
            raise ValueError('invalid_context')
        options={'deadline':deadline,'interrupted':interrupted}
        head=_git_bytes(['rev-parse','HEAD'],41,**options).decode().strip()
        if head!=build_commit:
            raise ValueError('invalid_context')
        _git_bytes(['merge-base','--is-ancestor',app_commit,build_commit],1,**options)
        application=_unpack(_git_archive(app_commit,**options),**options)
        environment=_unpack(_git_archive(build_commit,**options),**options)
        payload={name:content for name,content in application.items() if name.startswith('app/')}
        payload.update({name:environment[name] for name in ('Dockerfile','requirements.txt')})
        if sum(map(len,payload.values()))>4*1024*1024 or len(payload)>256:
            raise ValueError('invalid_context')
        files={name:hashlib.sha256(content).hexdigest() for name,content in sorted(payload.items())}
        descriptor={'schema_version':1,'app_commit':app_commit,'build_commit':build_commit,'files':files,
                    'payload_digest':hashlib.sha256(_canonical(files)).hexdigest(),
                    'main_sha':files['app/main.py'],'service_sha':files['app/services/book_service.py']}
        budget(deadline,interrupted);destination.mkdir(mode=0o700)
        for name,content in payload.items():
            budget(deadline,interrupted)
            target=destination/name;target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('xb') as output:output.write(content)
        with (destination/'release.json').open('xb') as output:output.write(_canonical(descriptor))
        return descriptor
    except (OSError,KeyError,TypeError,UnicodeError):
        raise ValueError('invalid_context') from None


def metadata(deadline,interrupted):
    options={'deadline':deadline,'interrupted':interrupted}
    commit=_git_bytes(['rev-parse','HEAD'],41,**options).decode().strip()
    dirty=bool(_git_bytes(['status','--porcelain'],1024*1024,**options))
    files=['book-memo/tools/container_release_lab.py','book-memo/tools/container_runtime.py',
           'book-memo/tools/container_probe.py','book-memo/tools/release_artifact.py']
    return {'git_sha':commit,'dirty':dirty,'python':platform.python_version(),'os':platform.system(),
            'architecture':platform.machine(),
            'source_hashes':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in files}}


def valid_state(state,version):
    return (all(type(state.get(key)) is int for key in ('book_count','memo_count','memo_version','revision_count','request_count'))
            and all(isinstance(state.get(key),str) and SHA.fullmatch(state[key]) for key in
                ('snapshot_hash','schema_hash','book_hash','supporting_hash'))
            and state.get('integrity_ok') is True and state.get('content_valid') is True
            and state.get('revisions_valid') is True and state.get('book_count')==1
            and state.get('memo_count')==1 and state.get('memo_version')==version
            and state.get('revision_count')==version-1 and state.get('request_count')==version-1)


def unchanged(before,after):
    return all(before.get(key)==after.get(key) for key in ('snapshot_hash','schema_hash','book_hash','supporting_hash'))


def supporting_preserved(before,after):
    return all(before.get(key)==after.get(key) for key in ('schema_hash','book_hash','supporting_hash'))


def ready(engine,container,deadline,interrupted):
    limit=min(deadline,time.monotonic()+30)
    while True:
        budget(deadline,interrupted)
        if time.monotonic()>=limit:raise Stopped('readiness_timeout')
        health=engine.probe(container,'health');probe=engine.probe(container,'ready')
        if health.get('valid') is True and probe.get('valid') is True and health.get('http_status')==200 and probe.get('http_status')==200:
            return {'health_status':200,'readiness_status':200}
        time.sleep(min(.2,max(0,limit-time.monotonic())))


def run_case(engine,images,scenario,deadline,interrupted):
    result={'scenario':scenario,'valid':False,'reason':'initialization','events':[],
            'observations':{},'instances':[],'deployment_succeeded':False,'rollback_succeeded':None,
            'rollback_attempts':0,'active_version':None,'v1_reads_v2':False,'v1_writes_after_rollback':False,
            'schema_unchanged':False,'data_verified':False}
    started=time.monotonic()
    def event(name):
        result['events'].append({'event':name,'elapsed_seconds':time.monotonic()-started,
                                'at':datetime.now(timezone.utc).isoformat()})
    def start(role,volume):
        budget(deadline,interrupted)
        image=images[role];container=engine.start(image,volume,role)
        instance={'role':role,'image_id':image['id'],'container_id':container,'stopped_cleanly':False}
        result['instances'].append(instance);event(role+'_started')
        state=ready(engine,container,deadline,interrupted)
        identity=engine.probe(container,'identity')
        if (identity.get('http_status')!=200 or identity.get('main_sha')!=image['main_sha']
                or identity.get('service_sha')!=image['service_sha']
                or identity.get('trash_purge_route') is not (role=='v2')):
            raise Stopped('runtime_identity_mismatch')
        result['observations'][role+'_identity']=identity
        event(role+'_ready');result['active_version']=role
        return container,instance,state
    def stop(container,instance):
        budget(deadline,interrupted);engine.stop(container)
        instance['stopped_cleanly']=True;result['active_version']=None;event(instance['role']+'_stopped')
    def read(container):
        observed=engine.probe(container,'read')
        if observed.get('valid') is not True or observed.get('http_status')!=200:
            raise Stopped('functional_read_failed')
        return observed
    try:
        budget(deadline,interrupted);volume=engine.volume();event('owned_volume_created')
        container,instance,probe=start('v1',volume)
        seed=engine.probe(container,'seed')
        if not valid_state(seed,1):raise Stopped('baseline_data_invalid')
        result['observations']['baseline']=seed;result['observations']['baseline_probe']=probe
        result['observations']['baseline_read']=read(container);event('baseline_verified')
        stop(container,instance)
        container,instance,probe=start('v2',volume)
        initial=engine.probe(container,'snapshot')
        result['observations']['v2_initial']=initial;result['observations']['v2_probe']=probe
        if not valid_state(initial,1) or not unchanged(seed,initial):
            raise Stopped('schema_or_data_changed_at_startup')
        result['schema_unchanged']=True;event('v2_initial_data_verified')
        if scenario=='readiness_failure':
            rejected=engine.probe(container,'missing');result['observations']['rejected_probe']=rejected
            if rejected.get('http_status')!=404 or rejected.get('valid') is not False:
                raise Stopped('expected_readiness_failure_missing')
            event('configured_readiness_rejected');post_v2=seed
        else:
            post_v2=engine.probe(container,'write_v2');result['observations']['v2_after_write']=post_v2
            if not valid_state(post_v2,2) or not supporting_preserved(seed,post_v2) or post_v2.get('replay_verified') is not True:
                raise Stopped('v2_write_invalid')
            result['observations']['v2_read']=read(container);event('v2_write_verified')
            result['deployment_succeeded']=True
        if scenario in ('roundtrip','readiness_failure'):
            stop(container,instance);result['rollback_attempts']=1;result['rollback_succeeded']=False
            event('rollback_started');container,instance,probe=start('v1',volume)
            restored=engine.probe(container,'snapshot');result['observations']['v1_after_rollback']=restored
            result['observations']['rollback_probe']=probe
            expected=2 if scenario=='roundtrip' else 1
            if not valid_state(restored,expected) or not unchanged(post_v2,restored):
                raise Stopped('rollback_data_invalid')
            result['observations']['rollback_read']=read(container)
            if scenario=='roundtrip':
                result['v1_reads_v2']=True
                rewritten=engine.probe(container,'write_v1');result['observations']['v1_after_write']=rewritten
                if not valid_state(rewritten,3) or not supporting_preserved(seed,rewritten) or rewritten.get('replay_verified') is not True:
                    raise Stopped('v1_write_after_rollback_invalid')
                result['observations']['v1_final_read']=read(container)
                result['v1_writes_after_rollback']=True;event('v1_backward_write_verified')
            result['rollback_succeeded']=True;event('rollback_verified')
        result['data_verified']=True;event('data_verified');stop(container,instance)
        result['valid'],result['reason']=True,'ok'
    except Stopped as error:
        result['reason']=str(error)
    except Exception:
        result['reason']=('interrupted' if interrupted() else 'deadline' if time.monotonic()>=deadline else 'container_operation_failed')
    if not result['valid']:result['deployment_succeeded']=False
    result['timings']={'total_seconds':time.monotonic()-started}
    return result


def run_lab(scenarios,repeats,deadline,interrupted,progress=None,engine_factory=None):
    report={'schema_version':1,'valid':False,'reason':'initialization','cases':[],'images':{},'contexts':{},
            'scope':'actual_two_app_versions_current_build_environment_container_internal_http_not_production',
            'started_at':datetime.now(timezone.utc).isoformat(),'remaining_cases':len(scenarios)*repeats,
            'fixture_removed':False,'cleanup':{'ok':False},'stage':'initialization',
            'configuration':{'scenarios':scenarios,'repeats':repeats,'build_cap_seconds':300,'cleanup_cap_seconds':90},
            'retained_cache':'base_images_and_build_cache_are_not_pruned'}
    root=None;engine=None
    def save():
        if progress is not None:progress(report)
    try:
        budget(deadline,interrupted);report['environment']=metadata(deadline,interrupted);save()
        root=Path(tempfile.mkdtemp(prefix='container-fixture-')).resolve();owner=secrets.token_hex(16)
        (root/'.container-lab-owner').write_text(owner)
        factory=engine_factory or LocalDocker;engine=factory(owner,deadline,interrupted)
        report['docker']=engine.info();save()
        current=report['environment']['git_sha']
        for role,commit in [('v1',OLD_COMMIT),('v2',current)]:
            context=prepare_context(root/role,commit,current,deadline,interrupted)
            report['contexts'][role]=context;report['stage']='build_'+role;save()
            image=engine.build(root/role,context,role)
            if any(image.get(key)!=context[key] for key in ('app_commit','build_commit','main_sha','service_sha')):
                raise Stopped('image_provenance_mismatch')
            report['images'][role]=image;save()
        if (report['images']['v1']['id']==report['images']['v2']['id']
                or report['contexts']['v1']['main_sha']==report['contexts']['v2']['main_sha']):
            raise Stopped('app_versions_not_distinct')
        for scenario in scenarios:
            for repeat in range(1,repeats+1):
                budget(deadline,interrupted)
                report['stage']=scenario+'_'+str(repeat);save()
                case=run_case(engine,report['images'],scenario,deadline,interrupted)
                case['repeat']=repeat;report['cases'].append(case);report['remaining_cases']-=1;save()
                if not case['valid']:raise Stopped(case['reason'])
        budget(deadline,interrupted);report['valid'],report['reason']=True,'ok'
    except Stopped as error:
        report['reason']=str(error)
    except Exception:
        report['reason']=('interrupted' if interrupted() else 'deadline' if time.monotonic()>=deadline else 'container_lab_failed')
    finally:
        report['stage']='cleanup';save()
        if engine is not None:
            try:report['cleanup']=engine.cleanup()
            except Exception:report['cleanup']={'ok':False,'reason':'cleanup_failed'}
        else:report['cleanup']={'ok':True,'containers_removed':0,'volumes_removed':0,'images_removed':0,
                               'residual_counts':{'containers':0,'volumes':0,'images':0}}
        if root is None:report['fixture_removed']=True
        elif report['cleanup'].get('ok') is True:
            try:shutil.rmtree(root);report['fixture_removed']=not root.exists()
            except OSError:pass
        if not report['cleanup'].get('ok') or not report['fixture_removed']:
            report['valid'],report['reason']=False,'cleanup_failed'
        if interrupted():
            report['valid']=False
            if report['cleanup'].get('ok') and report['fixture_removed']:report['reason']='interrupted'
        report['finished_at']=datetime.now(timezone.utc).isoformat();report['stage']='finished';save()
    return report


def arguments(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true');parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--scenario',choices=SCENARIOS,action='append')
    parser.add_argument('--repeats',type=int,default=1);parser.add_argument('--deadline',type=float,default=900)
    args=parser.parse_args(argv);args.scenario=args.scenario or list(SCENARIOS)
    if not args.run or not 1<=args.repeats<=3 or not 0<args.deadline<=1800 or len(set(args.scenario))!=len(args.scenario):
        parser.error('explicit --run, repeats1..3, deadline>0..1800 and unique scenarios required')
    return args


def main(argv=None):
    args=arguments(argv);interrupted=False
    def stop(_sig,_frame):
        nonlocal interrupted
        interrupted=True
    previous={sig:signal.signal(sig,stop) for sig in (signal.SIGINT,signal.SIGTERM)}
    deadline=time.monotonic()+args.deadline
    try:
        with args.output.open('x',encoding='utf-8') as output:
            def save(report):
                output.seek(0);json.dump(report,output,ensure_ascii=False,indent=2,allow_nan=False)
                output.truncate();output.flush()
            save({'schema_version':1,'valid':False,'reason':'initialization','cases':[],
                  'remaining_cases':len(args.scenario)*args.repeats})
            report=run_lab(args.scenario,args.repeats,deadline,lambda:interrupted,save)
    except OSError:
        print('container_lab=FAIL reason=output_unavailable');return 1
    finally:
        for sig,handler in previous.items():signal.signal(sig,handler)
    print(f"container_lab={'PASS' if report['valid'] else 'FAIL'} cases={len(report['cases'])} reason={report['reason']}")
    return 0 if report['valid'] else 1


if __name__=='__main__':raise SystemExit(main())
