"""PostgreSQL source, nonroot configuration and effective layer integrity boundaries."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ha_postgres_package', ROOT/'infra/k8s/ha-package/postgres.py')
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)


class PostgresPackageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'repo'
        self.root.mkdir()
        for name in pg.SOURCES:
            target = self.root/name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT/name).read_bytes())
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        self.bundle = Path(self.temp.name)/'bundle'
        context = patch.object(pg.P, 'ROOT', self.root)
        context.start()
        self.addCleanup(context.stop)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True, stderr=subprocess.DEVNULL)

    def archive(self, *, deleted=True, reintroduced=False, user='70:70'):
        manifest = pg.prepare(self.commit, self.bundle)
        owner = 'fixture'
        config = {'architecture': 'amd64', 'os': 'linux', 'config': {
            'User': user, 'Entrypoint': ['docker-entrypoint.sh'], 'Cmd': ['postgres'],
            'Env': ['PG_MAJOR=17', 'PG_VERSION=17.11'], 'Labels': {
                pg.OWNER: owner, 'org.opencontainers.image.revision': self.commit,
                'personal-server.ha-release': manifest['release_id']}}}
        layers = []
        for name in ['usr/local/bin/gosu'] + (['usr/local/bin/.wh.gosu'] if deleted else []) + (['usr/local/bin/gosu'] if reintroduced else []):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode='w') as layer:
                info = tarfile.TarInfo(name)
                layer.addfile(info, io.BytesIO())
            layers.append(buffer.getvalue())
        config['rootfs'] = {'type': 'layers', 'diff_ids': ['sha256:'+pg.P.sha(data) for data in layers]}
        raw = pg.P.encode(config)
        image_id = 'sha256:'+pg.P.sha(raw)
        names = [str(i)+'/layer.tar' for i in range(len(layers))]
        members = {'manifest.json': pg.P.encode([{'Config': 'config.json', 'Layers': names}]), 'config.json': raw}
        members.update(dict(zip(names, layers)))
        with tarfile.open(self.bundle/'image.tar', 'w') as archive:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        receipt = pg.P.archive_binding(self.bundle/'image.tar', image_id)
        receipt.update(source_commit=self.commit, release_id=manifest['release_id'], owner=owner, smoke='PASS',
                       user='70:70', version='postgres (PostgreSQL) 17.11', gosu_removed=True,
                       pgcrypto_sha256=pg.P.sha(b'ha-postgres-smoke'))
        (self.bundle/'image-bindings.json').write_bytes(pg.P.encode(receipt))
        return manifest

    def test_recipe_retains_official_entrypoint_and_pins_security_updates(self):
        manifest = pg.prepare(self.commit, self.bundle)
        self.assertEqual((self.bundle/'Dockerfile').read_text(), pg.RECIPE)
        self.assertEqual(set(manifest['source_files']), set(pg.SOURCES))
        self.assertIn('@sha256:', manifest['base'])
        self.assertNotIn('ENTRYPOINT', pg.RECIPE)
        self.assertIn("'krb5-libs>=1.22.3-r0'", pg.RECIPE)
        self.assertIn("'libxslt>=1.1.43-r4'", pg.RECIPE)
        self.assertLess(pg.RECIPE.index('apk add'), pg.RECIPE.index('USER 70:70'))
        self.assertTrue(pg.verify_source(self.bundle))

    def test_dirty_working_tree_is_not_release_source(self):
        (self.root/pg.SOURCES[0]).write_text('FROM postgres:latest\n')
        pg.prepare(self.commit, self.bundle)
        self.assertEqual((self.bundle/'Dockerfile').read_text(), pg.RECIPE)
        with self.assertRaises(pg.P.PackageError):
            pg.prepare('HEAD', self.bundle/'other')

    def test_recipe_mutation_and_manifest_tamper_rejected(self):
        files = pg.snapshot(self.commit)
        files[pg.SOURCES[0]] += b'USER root\n'
        with self.assertRaises(pg.P.PackageError):
            pg.recipe(files)
        pg.prepare(self.commit, self.bundle)
        (self.bundle/'Dockerfile').chmod(0o644)
        (self.bundle/'Dockerfile').write_text('FROM postgres:latest\n')
        with self.assertRaises(pg.P.PackageError):
            pg.verify_source(self.bundle)

    def test_classic_archive_with_gosu_whiteout_is_verified(self):
        self.archive()
        self.assertTrue(pg.verify(self.bundle)['verified'])

    def test_retained_gosu_and_reintroduction_are_rejected(self):
        for deleted, reintroduced in [(False, False), (True, True)]:
            with self.subTest(deleted=deleted, reintroduced=reintroduced):
                self.bundle = Path(self.temp.name)/('bundle-'+str(deleted)+str(reintroduced))
                self.archive(deleted=deleted, reintroduced=reintroduced)
                with self.assertRaisesRegex(pg.P.PackageError, 'gosu_removal'):
                    pg.verify(self.bundle)

    def test_archive_root_user_and_forged_smoke_are_rejected(self):
        self.archive(user='0:0')
        with self.assertRaisesRegex(pg.P.PackageError, 'config_binding'):
            pg.verify(self.bundle)
        receipt = json.loads((self.bundle/'image-bindings.json').read_bytes())
        receipt['pgcrypto_sha256'] = '0'*64
        (self.bundle/'image-bindings.json').write_bytes(pg.P.encode(receipt))
        with self.assertRaisesRegex(pg.P.PackageError, 'archive_binding'):
            pg.verify(self.bundle)


if __name__ == '__main__':
    unittest.main()
