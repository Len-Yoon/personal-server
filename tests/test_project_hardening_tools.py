import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'infra/k8s/tools'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

class HardeningToolsTests(unittest.TestCase):
    def test_guarded_patch_preserves_images_volumes_and_existing_resources(self):
        snapshot={'kind':'Deployment','metadata':{'name':'portal-web','namespace':'personal-server','uid':'opaque','resourceVersion':'3'},'spec':{'template':{'spec':{'containers':[{'name':'portal-web','image':'same@sha256:123','resources':{'requests':{'cpu':'75m'}},'readinessProbe':{'httpGet':{'path':'/health','port':8000}},'volumeMounts':[{'name':'data','mountPath':'/data'}]}]}}}}
        before=json.dumps(snapshot,sort_keys=True)
        result=load('prepare-app-hardening').prepare(snapshot)
        self.assertEqual(json.dumps(snapshot,sort_keys=True),before)
        self.assertEqual([p['op'] for p in result['patch'][:3]],['test']*3)
        self.assertFalse(any('/image' in p['path'] or '/volume' in p['path'] or p['path'].endswith('/requests/cpu') for p in result['patch']))
        self.assertTrue(result['approval_required'])
        snapshot['metadata']['namespace']='monitoring'
        with self.assertRaises(ValueError):load('prepare-app-hardening').prepare(snapshot)

    def test_exact_git_context_excludes_secrets_data_and_working_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'repo';root.mkdir()
            def git(*args):return subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True,text=True).stdout.strip()
            git('init');git('config','user.email','test@example.invalid');git('config','user.name','Fixture')
            app=root/'book-memo';(app/'app').mkdir(parents=True)
            (app/'Dockerfile').write_text('FROM scratch\nCOPY app /app\n')
            (app/'requirements.txt').write_text('')
            (app/'app/main.py').write_text('original')
            (app/'data').mkdir();(app/'data/private.sqlite3').write_text('private')
            (app/'.env').write_text('SECRET=private')
            git('add','.');git('commit','-m','fixture');revision=git('rev-parse','HEAD')
            (app/'app/main.py').write_text('working')
            target=Path(temp)/'context';target.mkdir()
            load('build-verified-image').build_context(root,'book-memo',revision,target)
            self.assertEqual((target/'app/main.py').read_text(),'original')
            self.assertFalse((target/'.env').exists());self.assertFalse((target/'data').exists())
            with self.assertRaises(ValueError):load('build-verified-image').build_context(root,'book-memo','main',target)

    def test_publish_never_overwrites_and_removes_partial_pair(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);archive=root/'source.tar';archive.write_bytes(b'complete image')
            output=root/'image.tar';module=load('build-verified-image')
            sidecar=root/'image.tar.json';sidecar.write_text('existing')
            with self.assertRaises(FileExistsError):module.publish_archive(archive,output,{'scan':'passed'})
            self.assertFalse(output.exists());self.assertEqual(sidecar.read_text(),'existing')
            sidecar.unlink()
            original=module.os.link
            calls=[]
            def race(source,target):
                calls.append(target)
                if len(calls)==2:raise FileExistsError('race')
                return original(source,target)
            with patch.object(module.os,'link',side_effect=race):
                with self.assertRaises(FileExistsError):module.publish_archive(archive,output,{'scan':'passed'})
            self.assertFalse(output.exists());self.assertFalse(sidecar.exists())
            module.publish_archive(archive,output,{'scan':'passed'})
            self.assertEqual(output.read_bytes(),archive.read_bytes())
            self.assertEqual(json.loads(sidecar.read_text())['scan'],'passed')

    def test_runtime_backup_validation_matches_authoritative_contract(self):
        import ast
        authoritative=ast.parse((ROOT/'infra/k8s/tools/validate-backup-evidence.py').read_text())
        runtime=ast.parse((ROOT/'system-agent/app/services/backup_evidence.py').read_text())
        names={'parse_timestamp','parse_evidence','validate_evidence'}
        functions=lambda tree:{n.name:ast.dump(n,include_attributes=False) for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names}
        self.assertEqual(functions(authoritative),functions(runtime))

    def test_build_exports_oci_annotation_and_requires_scan_before_publish(self):
        from argparse import Namespace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            module=load('build-verified-image');output=Path(temp)/'app.oci.tar'
            args=Namespace(app='book-memo',revision='a'*40,output=output)
            calls=[]
            def runner(command,check):
                self.assertTrue(check);calls.append(command)
                if command[0]=='docker':
                    target=command[command.index('--output')+1]
                    self.assertTrue(target.startswith('type=oci,dest='))
                    self.assertIn('annotation-manifest-descriptor.io.personal-server.image-ref=personal-server-book-memo:',target)
                    Path(target.split('dest=',1)[1].split(',',1)[0]).write_bytes(b'fixture OCI')
                else:
                    self.assertEqual(command[:3],['trivy','image','--input'])
                    self.assertIn('--exit-code',command)
                    raise subprocess.CalledProcessError(1,command)
            with patch.object(module.argparse.ArgumentParser,'parse_args',return_value=args), patch.object(module,'build_context'), patch.object(module.subprocess,'run',side_effect=runner):
                with self.assertRaises(subprocess.CalledProcessError):module.main()
            self.assertEqual(len(calls),2)
            self.assertFalse(output.exists());self.assertFalse(output.with_suffix(output.suffix+'.json').exists())
