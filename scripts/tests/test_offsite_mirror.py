"""Run the real shell with isolated paths and a fake mc; no bucket or host cleanup."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'infra/backup/offsite-mirror.sh'


# Verified with encoding/json on the complete official minio-go v7.0.90 lifecycle.go.
# https://github.com/minio/minio-go/blob/v7.0.90/pkg/lifecycle/lifecycle.go
# mc RELEASE.2025-04-16T18-13-26Z cmd/ilm-rule-{import,export}.go reject zero rules.
def policy(days=7, **changes):
    rule = {'Expiration': {'Days': days}, 'ID': 'memoria-offsite-retention',
            'NoncurrentVersionExpiration': {'NoncurrentDays': days}, 'Status': 'Enabled'}
    rule.update(changes)
    return {'Rules': [rule]}


class OffsiteMirrorTest(unittest.TestCase):
    def run_mirror(self, initial=None, failure='', days=None, drift=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            for name in ['tr', 'grep']:
                (bin_dir / name).symlink_to(shutil.which(name))
            staging = root / 'staging'
            staging.mkdir()
            (staging / 'LATEST').write_text('20261112T100000Z\n')
            (root / 'wal').mkdir()
            state = root / 'state.json'
            state.write_text(json.dumps(initial or {'Rules': []}))
            log = root / 'calls'
            mc = bin_dir / 'mc'
            mc.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['CALLS'], 'a') as f: f.write(' '.join(args) + '\\n')
state = Path(os.environ['STATE'])
failure = os.environ['FAILURE']
if args[:2] == ['version', 'info']: print('Enabled')
elif args[:3] == ['ilm', 'rule', 'import']:
    data = json.load(sys.stdin)
    if not data.get('Rules'):
        print('The provided ILM configuration does not contain any rule, aborting.', file=sys.stderr)
        sys.exit(1)
    if failure == 'create': sys.exit(3)
    if failure != 'ignored': state.write_text(json.dumps(data))
elif args[:3] == ['ilm', 'rule', 'export']:
    if failure == 'query': print(state.read_text()); sys.exit(4)
    data = json.loads(state.read_text())
    if failure == 'malformed': print('Expiry'); sys.exit(0)
    if not data.get('Rules'):
        print('Unable to export lifecycle configuration. lifecycle configuration not set.', file=sys.stderr)
        sys.exit(1)
    print(json.dumps(data, indent=1))
    if os.environ['DRIFT'] == 'yes': state.write_text(json.dumps({'Rules': []}))
elif args[0] == 'mirror' and failure == 'mirror': sys.exit(5)
''')
            mc.chmod(0o755)
            for command, body in {
                'date': 'echo 2026-11-12T10:00:00Z',
                'find': 'echo cleanup >> "$CALLS"',
                'sleep': 'exit 9' if not drift else 'exit 0',
            }.items():
                executable = bin_dir / command
                executable.write_text('#!/bin/sh\n' + body + '\n')
                executable.chmod(0o755)
            source = SCRIPT.read_text().replace('/backup-staging', str(staging)).replace(
                '/wal-archive', str(root / 'wal')).replace(
                '/tmp/offsite-mirror.last-success', str(root / 'success'))
            script = root / 'mirror.sh'
            script.write_text(source)
            env = {
                'PATH': str(bin_dir), 'CALLS': str(log), 'STATE': str(state),
                'FAILURE': failure, 'DRIFT': 'yes' if drift else 'no',
                'MINIO_ROOT_USER': 'fixture', 'MINIO_ROOT_PASSWORD': 'fixture',
                'MEMORIA_OFFSITE_S3_ENDPOINT': 'https://fixture.invalid',
                'MEMORIA_OFFSITE_S3_ACCESS_KEY': 'fixture',
                'MEMORIA_OFFSITE_S3_SECRET_KEY': 'fixture',
                'MEMORIA_OFFSITE_S3_BUCKET': 'fixture',
            }
            if days is not None:
                env['MEMORIA_OFFSITE_RETENTION_DAYS'] = str(days)
            result = subprocess.run(['/bin/sh', str(script)], env=env, text=True,
                                    capture_output=True, timeout=5)
            return result, log.read_text() if log.exists() else '', (
                (root / 'success').read_text() if (root / 'success').exists() else '')

    def test_creation_and_query_fail_closed(self):
        for failure in ['create', 'query', 'malformed']:
            with self.subTest(failure=failure):
                result, calls, success = self.run_mirror(failure=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('mirror ', calls)
                self.assertNotIn('cleanup', calls)
                self.assertEqual(success, '')

    def test_unqualified_readback_fail_closed(self):
        for initial in [policy(30), policy(Status='Disabled'),
                        policy(Filter={'Prefix': 'partial/'}),
                        policy(NoncurrentVersionExpiration={'NoncurrentDays': 30}),
                        policy(NoncurrentVersionExpiration={'NoncurrentDays': 7, 'NewerNoncurrentVersions': 2}),
                        policy(Filter={'Tag': {'Key': 'x', 'Value': 'y'}})]:
            with self.subTest(initial=initial):
                _, calls, success = self.run_mirror(initial, failure='ignored')
                self.assertNotIn('mirror ', calls)
                self.assertEqual(success, '')

    def test_default_and_explicit_days_sync_only_after_verified_configuration(self):
        for days in [None, *range(1, 8)]:
            with self.subTest(days=days):
                result, calls, success = self.run_mirror(policy(days or 7), days=days)
                self.assertEqual(result.returncode, 9, result.stderr)
                self.assertIn('ilm rule export', calls)
                self.assertEqual(calls.count('mirror --'), 4)
                self.assertLess(calls.index('ilm rule export'), calls.index('mirror --'))
                self.assertEqual(success, '2026-11-12T10:00:00Z\n')

    def test_noncompliant_existing_config_is_replaced_and_verified(self):
        _, calls, success = self.run_mirror(policy(30))
        self.assertIn('ilm rule import', calls)
        self.assertTrue(success)

    def test_mirror_failure_does_not_advance_watermark(self):
        _, calls, success = self.run_mirror(policy(), failure='mirror')
        self.assertIn('mirror --', calls)
        self.assertEqual(success, '')
        self.assertNotIn('cleanup', calls)

    def test_recheck_each_cycle_stops_after_drift(self):
        _, calls, success = self.run_mirror(policy(), drift=True)
        self.assertEqual(calls.count('ilm rule export'), 2)
        self.assertEqual(calls.count('mirror --'), 4)
        self.assertTrue(success)

    def test_invalid_retention_never_contacts_mc(self):
        for days in [0, 8, -1, 'bad']:
            _, calls, success = self.run_mirror(days=days)
            self.assertEqual(calls, '')
            self.assertEqual(success, '')


if __name__ == '__main__':
    unittest.main()
