"""Private runtime archive/RECORD validation; never native installation proof."""
import base64
import hashlib
import io
from pathlib import Path
import sys
import unittest
import zipfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'infra/k8s'))
from ha_control import enrollment as E


def archive(files):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        for name,raw in files.items():
            z.writestr(name,raw)
    return out.getvalue()


def wheel():
    raw=b'pass\n';digest=base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip('=')
    return {'fixture/__init__.py':raw,'fixture.dist-info/RECORD':f'fixture/__init__.py,sha256={digest},{len(raw)}\nfixture.dist-info/RECORD,,\n'.encode()}


class EnrollmentTests(unittest.TestCase):
    def test_unsafe_windows_names_collisions_symlinks_and_hooks_rejected(self):
        with self.assertRaises(E.EnrollmentError):E.member_name('a\x00b')
        for name in ('../x','/x','C:x','a\\b','a//b','a/./b','a/b.','a/b ','a/CON.txt','a/nul','a/x.pth'):
            with self.subTest(name=name),self.assertRaises(E.EnrollmentError):
                E.archive_files(archive({name:b'x'}))
        with self.assertRaises(E.EnrollmentError):
            E.archive_files(archive({'a.py':b'x','A.py':b'y'}))
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            item=zipfile.ZipInfo('link');item.external_attr=0o120777<<16;z.writestr(item,b'target')
        with self.assertRaises(E.EnrollmentError):
            E.archive_files(out.getvalue())

    def test_record_requires_exact_file_hash_size_and_complete_inventory(self):
        files=wheel();E.verify_record(files)
        for bad in (files|{'fixture/__init__.py':b'changed'},files|{'unlisted.py':b'pass'},
                    {k:v for k,v in files.items() if not k.endswith('RECORD')},
                    files|{'fixture.dist-info/RECORD':files['fixture.dist-info/RECORD']*2}):
            with self.subTest(files=list(bad)),self.assertRaises(E.EnrollmentError):
                E.verify_record(bad)

    def test_pinned_assembly_disables_site_and_rejects_wrong_artifact(self):
        python=archive({'python.exe':b'pe','python313.dll':b'dll','python313.zip':b'stdlib','python313._pth':b'import site\n'})
        whl=archive(wheel());inputs={E.PYTHON_FILE:python,'fixture.whl':whl}
        with patch.object(E,'PYTHON_SHA256',E.sha(python)),patch.object(E,'WHEELS',{'fixture.whl':E.sha(whl)}):
            result=E.assemble(inputs)
            self.assertEqual(result['python313._pth'],b'python313.zip\n.\nLib/site-packages\n')
            self.assertIn('Lib/site-packages/fixture/__init__.py',result)
            with self.assertRaises(E.EnrollmentError):E.assemble(inputs|{'fixture.whl':whl+b'x'})
            with self.assertRaises(E.EnrollmentError):E.assemble(inputs|{'unknown.whl':whl})


if __name__=='__main__':
    unittest.main()
