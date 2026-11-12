"""Run the real shell with isolated paths and a fake mc; no bucket or host cleanup."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'infra/backup/offsite-mirror.sh'
PRIMARY_SHA256 = 'bd63c09d507c03a64c5a9cbd89b2b921abb6a8b0715d02e9abbfa9bcddd71c70'

# Captured with encoding/json.MarshalIndent on the complete official source:
# https://github.com/minio/minio-go/blob/v7.0.90/pkg/lifecycle/lifecycle.go
# SHA256 bd63c09d507c03a64c5a9cbd89b2b921abb6a8b0715d02e9abbfa9bcddd71c70.
# The fixture is independent of offsite-mirror.sh's policy generator.
PINNED_EXPORT = '''{
 "Rules": [
  {
   "Expiration": {
    "Days": 7
   },
   "ID": "memoria-offsite-retention",
   "NoncurrentVersionExpiration": {
    "NoncurrentDays": 7
   },
   "Status": "Enabled"
  }
 ]
}'''


def policy(days=7, **changes):
    data = json.loads(PINNED_EXPORT)
    rule = data['Rules'][0]
    rule['Expiration']['Days'] = days
    rule['NoncurrentVersionExpiration']['NoncurrentDays'] = days
    rule.update(changes)
    return data


def verify_primary_contract(source):
    """Use an exact, separately downloaded upstream file. Never fetch or install."""
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != PRIMARY_SHA256:
        raise ValueError('expected the exact official minio-go v7.0.90 lifecycle.go')
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'lifecycle.go').write_bytes(data.replace(b'package lifecycle', b'package main', 1))
        (root / 'primary.go').write_text(r'''package main
import ("encoding/json"; "encoding/xml"; "fmt")
func main() {
 var cfg Configuration
 raw := `<LifecycleConfiguration><Rule><Expiration><Days>7</Days></Expiration><ID>memoria-offsite-retention</ID><NoncurrentVersionExpiration><NoncurrentDays>7</NoncurrentDays></NoncurrentVersionExpiration><Status>Enabled</Status></Rule></LifecycleConfiguration>`
 if err := xml.Unmarshal([]byte(raw), &cfg); err != nil { panic(err) }
 output, err := json.MarshalIndent(cfg, "", " "); if err != nil { panic(err) }
 counts := []int{}
 for _, input := range []string{`{"Rule":[{"ID":"memoria-offsite-retention"}]}`, `{"Rules":[]}`, string(output)} {
  decoded := NewConfiguration()
  if err := json.Unmarshal([]byte(input), decoded); err != nil { panic(err) }
  counts = append(counts, len(decoded.Rules))
 }
 proof, err := json.Marshal(struct{Export string; RuleCounts []int}{string(output), counts})
 if err != nil { panic(err) }; fmt.Println(string(proof))
}
''')
        env = dict(os.environ, GOWORK='off', GOPROXY='off', GOTOOLCHAIN='local', GO111MODULE='off')
        result = subprocess.run(['go', 'run', str(root / 'lifecycle.go'), str(root / 'primary.go')],
                                env=env, text=True, capture_output=True, timeout=60, check=True)
    proof = json.loads(result.stdout)
    if proof != {'Export': PINNED_EXPORT, 'RuleCounts': [0, 0, 1]}:
        raise AssertionError(f'primary serializer disagrees with fixture: {proof!r}')
    print(f'PASS official lifecycle.go SHA256 {PRIMARY_SHA256}')
    print('PASS exact MarshalIndent fixture; Rule=0 rules, empty Rules=0, valid Rules=1')


# Primary contracts at mc RELEASE.2025-04-16T18-13-26Z:
# cmd/ilm-rule-import.go rejects len(cfg.Rules)==0, including missing Rules.
# cmd/ilm-rule-export.go rejects zero rules. cmd/error.go supplies punctuation.
# cmd/version-info.go String() emits the lowercased versioning status.
FAKE_MC = r'''import json, os, sys
from pathlib import Path
args = [a for a in sys.argv[1:] if a != '--no-color']
with open(os.environ['CALLS'], 'a') as f:
    f.write(('alias set [redacted]' if args[:2] == ['alias', 'set'] else ' '.join(args)) + '\n')
state = Path(os.environ['STATE'])
imported = Path(os.environ['IMPORTED'])
failure = os.environ['FAILURE']
empty_error = 'mc: <ERROR> Unable to export lifecycle configuration: lifecycle configuration not set.'
if args[:2] == ['alias', 'set'] or args[0] == 'stat':
    pass
elif args[:2] == ['version', 'info']:
    print(os.environ['VERSION'])
    if failure == 'version': sys.exit(6)
elif args[:3] == ['ilm', 'rule', 'import']:
    data = json.load(sys.stdin)
    if not data.get('Rules'):
        print('mc: <ERROR> The provided ILM configuration does not contain any rule, aborting.', file=sys.stderr)
        sys.exit(1)
    if failure == 'create': sys.exit(3)
    imported.touch()
    if failure != 'ignored': state.write_text(json.dumps(data))
elif args[:3] == ['ilm', 'rule', 'export']:
    if failure == 'query' or (failure == 'readback' and imported.exists()):
        print(state.read_text())
        sys.exit(4)
    if failure.startswith('query_'):
        messages = {
            'query_permission': 'mc: <ERROR> Unable to get lifecycle configuration. Access Denied.',
            'query_network': 'mc: <ERROR> Unable to get lifecycle configuration. connection refused.',
            'query_empty_extra': empty_error + '\nAccess Denied.',
            'query_empty_bad_status': empty_error,
        }
        print(messages[failure], file=sys.stderr)
        sys.exit(4 if failure == 'query_empty_bad_status' else 1)
    raw = os.environ['READBACK'] if imported.exists() else os.environ['EXPORT']
    if raw:
        print(raw)
    else:
        data = json.loads(state.read_text())
        if not data.get('Rules'):
            if os.environ['EMPTY'] == 'missing':
                empty_error = 'mc: <ERROR> Unable to get lifecycle configuration. The lifecycle configuration does not exist.'
            print(empty_error, file=sys.stderr)
            sys.exit(1)
        print(json.dumps(data, indent=1))
elif args[0] == 'mirror':
    if failure == 'mirror': sys.exit(5)
elif args[0] == 'cp':
    if failure == 'copy': sys.exit(7)
else:
    print('unexpected fake mc operation', file=sys.stderr)
    sys.exit(99)
'''


class OffsiteMirrorTest(unittest.TestCase):
    def run_mirror(self, initial=None, failure='', days=None, drift=None,
                   watermark='', source=None, version='offsite/fixture versioning is enabled',
                   readback='', export='', empty='zero'):
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
            state.write_text(json.dumps(initial if initial is not None else {'Rules': []}))
            if watermark:
                (root / 'success').write_text(watermark)
            log = root / 'calls'
            mc = bin_dir / 'mc'
            mc.write_text('#!' + sys.executable + '\n' + FAKE_MC)
            mc.chmod(0o755)
            for command, body in {
                'date': 'echo 2026-11-12T10:00:00Z',
                'find': 'echo cleanup >> "$CALLS"',
                'sleep': 'echo sleep >> "$CALLS"\n' + (
                    'exit 9' if drift is None else 'printf "%s" "$DRIFT" > "$STATE"'),
            }.items():
                executable = bin_dir / command
                executable.write_text('#!/bin/sh\n' + body + '\n')
                executable.chmod(0o755)
            source = source if source is not None else SCRIPT.read_text()
            for old, new in [('/backup-staging', staging), ('/wal-archive', root / 'wal'),
                             ('/tmp/offsite-mirror.last-success', root / 'success')]:
                source = source.replace(old, str(new))
                self.assertNotIn(old, source)
            script = root / 'mirror.sh'
            script.write_text(source)
            env = {
                'PATH': str(bin_dir), 'CALLS': str(log), 'STATE': str(state),
                'IMPORTED': str(root / 'imported'), 'READBACK': readback, 'EXPORT': export,
                'EMPTY': empty, 'VERSION': version, 'FAILURE': failure, 'DRIFT': json.dumps(drift),
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
            return (result, log.read_text() if log.exists() else '',
                    (root / 'success').read_text() if (root / 'success').exists() else '',
                    json.loads(state.read_text()))

    def test_default_and_explicit_days_sync_only_after_verified_configuration(self):
        for days in [None, *range(1, 8)]:
            with self.subTest(days=days):
                result, calls, success, final = self.run_mirror(policy(days or 7), days=days)
                self.assertEqual(result.returncode, 9, result.stderr)
                self.assertEqual(calls.count('ilm rule export'), 2)
                self.assertLess(calls.index('ilm rule export'), calls.index('ilm rule import'))
                self.assertLess(calls.rindex('ilm rule export'), calls.index('mirror --'))
                self.assertEqual(calls.count('mirror --'), 4)
                self.assertEqual(final, policy(days or 7))
                self.assertEqual(success, '2026-11-12T10:00:00Z\n')

    def test_failed_or_unknown_version_output_cannot_import(self):
        for version, failure, code in [
                ('Enabled', '', 2), ('offsite/fixture versioning is suspended', '', 2),
                ('offsite/fixture is un-versioned', '', 2),
                ('offsite/fixture versioning is enabled', 'version', 6)]:
            with self.subTest(version=version, failure=failure):
                result, calls, success, final = self.run_mirror(policy(), version=version, failure=failure)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn('version', result.stderr)
                self.assertNotIn('ilm rule', calls)
                self.assertNotIn('mirror ', calls)
                self.assertEqual(success, '')
                self.assertEqual(final, policy())

    def test_foreign_and_mixed_rules_preserve_policy_and_watermark(self):
        foreign = policy(30, ID='logs-30d')
        for initial in [foreign, {'Rules': policy()['Rules'] + foreign['Rules']},
                        {'Rules': foreign['Rules'] + policy()['Rules']},
                        policy(ID='memoria-offsite- retention'),
                        {'Rules': policy()['Rules'] * 2}]:
            with self.subTest(initial=initial):
                result, calls, success, final = self.run_mirror(initial, watermark='previous-success\n')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('ownership', result.stderr)
                self.assertNotIn('ilm rule import', calls)
                self.assertNotIn('mirror ', calls)
                self.assertNotIn('cleanup', calls)
                self.assertEqual(final, initial)
                self.assertEqual(success, 'previous-success\n')

    def test_empty_configuration_can_be_created_after_export(self):
        for empty in ['zero', 'missing']:
            with self.subTest(empty=empty):
                result, calls, success, final = self.run_mirror(empty=empty)
                self.assertEqual(result.returncode, 9, result.stderr)
                self.assertEqual(calls.count('ilm rule export'), 2)
                self.assertLess(calls.index('ilm rule export'), calls.index('ilm rule import'))
                self.assertEqual(final, policy())
                self.assertTrue(success)

    def test_owned_policy_can_be_adjusted_to_selected_days(self):
        for days in range(1, 8):
            result, calls, success, final = self.run_mirror(policy(30), days=days)
            self.assertEqual(result.returncode, 9, result.stderr)
            self.assertEqual(final, policy(days))
            self.assertEqual(calls.count('ilm rule import'), 1)
            self.assertTrue(success)

    def test_unknown_export_format_cannot_authorize_import(self):
        for raw in ['Expiry', json.dumps(policy()), PINNED_EXPORT.replace('"Rules"', '"Rule"'),
                    PINNED_EXPORT.replace('"Days": 7', '"Days": "7"')]:
            with self.subTest(raw=raw):
                result, calls, success, final = self.run_mirror(policy(), export=raw)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('ownership', result.stderr)
                self.assertNotIn('ilm rule import', calls)
                self.assertNotIn('mirror ', calls)
                self.assertEqual(final, policy())
                self.assertEqual(success, '')

    def test_query_failures_cannot_be_mistaken_for_an_empty_policy(self):
        for failure, code in [('query', 4), ('query_permission', 1), ('query_network', 1),
                              ('query_empty_extra', 1), ('query_empty_bad_status', 4)]:
            with self.subTest(failure=failure):
                result, calls, success, final = self.run_mirror(policy(), failure=failure, watermark='previous\n')
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn('ownership query failed', result.stderr)
                self.assertEqual(calls.count('ilm rule export'), 1)
                self.assertNotIn('ilm rule import', calls)
                self.assertNotIn('mirror ', calls)
                self.assertNotIn('cleanup', calls)
                self.assertEqual(final, policy())
                self.assertEqual(success, 'previous\n')

    def test_import_failure_preserves_policy_and_watermark(self):
        result, calls, success, final = self.run_mirror(policy(30), failure='create', watermark='previous\n')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(calls.count('ilm rule export'), 1)
        self.assertEqual(calls.count('ilm rule import'), 1)
        self.assertNotIn('mirror ', calls)
        self.assertNotIn('cleanup', calls)
        self.assertEqual(final, policy(30))
        self.assertEqual(success, 'previous\n')

    def test_wrong_rule_and_zero_rule_imports_are_rejected_by_mc_contract(self):
        for payload in [{'Rule': policy()['Rules']}, {'Rules': []}, {}]:
            with self.subTest(payload=payload):
                source = SCRIPT.read_text()
                original = 'printf \'%s\\n\' "$expected_ilm" | mc ilm rule import'
                replacement = 'printf \'%s\\n\' \'' + json.dumps(payload) + '\' | mc ilm rule import'
                self.assertIn(original, source)
                result, calls, success, final = self.run_mirror(policy(30), source=source.replace(original, replacement))
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('does not contain any rule', result.stderr)
                self.assertEqual(calls.count('ilm rule export'), 1)
                self.assertEqual(calls.count('ilm rule import'), 1)
                self.assertNotIn('mirror ', calls)
                self.assertEqual(final, policy(30))
                self.assertEqual(success, '')

    def test_readback_query_failure_stops_before_mirror(self):
        result, calls, success, _ = self.run_mirror(policy(), failure='readback', watermark='previous\n')
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn('readback query failed', result.stderr)
        self.assertEqual(calls.count('ilm rule export'), 2)
        self.assertEqual(calls.count('ilm rule import'), 1)
        self.assertNotIn('mirror ', calls)
        self.assertNotIn('cleanup', calls)
        self.assertEqual(success, 'previous\n')

    def test_unqualified_readback_fail_closed(self):
        for data in [policy(30), policy(Status='Disabled'), policy(Filter={'Prefix': 'partial/'}),
                     policy(NoncurrentVersionExpiration={'NoncurrentDays': 30}),
                     policy(NoncurrentVersionExpiration={'NoncurrentDays': 7, 'NewerNoncurrentVersions': 2}),
                     policy(Filter={'Tag': {'Key': 'x', 'Value': 'y'}})]:
            with self.subTest(data=data):
                result, calls, success, _ = self.run_mirror(policy(), readback=json.dumps(data, indent=1))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('readback differs', result.stderr)
                self.assertEqual(calls.count('ilm rule export'), 2)
                self.assertEqual(calls.count('ilm rule import'), 1)
                self.assertNotIn('mirror ', calls)
                self.assertNotIn('cleanup', calls)
                self.assertEqual(success, '')

    def test_unknown_readback_format_has_a_diagnostic(self):
        result, calls, success, _ = self.run_mirror(policy(), readback='Expiry')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('unknown format', result.stderr)
        self.assertNotIn('mirror ', calls)
        self.assertEqual(success, '')

    def test_ignored_import_cannot_sync_the_previous_owned_30_day_policy(self):
        result, calls, success, final = self.run_mirror(policy(30), failure='ignored')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(calls.count('ilm rule export'), 2)
        self.assertEqual(final, policy(30))
        self.assertNotIn('mirror ', calls)
        self.assertEqual(success, '')

    def test_mirror_and_copy_failure_do_not_advance_watermark(self):
        for failure, code, copied in [('mirror', 5, False), ('copy', 7, True)]:
            with self.subTest(failure=failure):
                result, calls, success, _ = self.run_mirror(policy(), failure=failure, watermark='previous\n')
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(calls.count('ilm rule export'), 2)
                self.assertEqual(calls.count('mirror --'), 1)
                self.assertEqual('cp --quiet' in calls, copied)
                self.assertEqual(success, 'previous\n')
                self.assertNotIn('cleanup', calls)
                self.assertNotIn('sleep', calls)

    def test_recheck_each_cycle_stops_with_code_2_after_drift(self):
        foreign = policy(30, ID='logs-30d')
        for drift in [{'Rules': []}, policy(30), foreign,
                      {'Rules': policy()['Rules'] + foreign['Rules']}]:
            with self.subTest(drift=drift):
                result, calls, success, final = self.run_mirror(policy(), drift=drift)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(calls.count('ilm rule export'), 3)
                self.assertEqual(calls.count('ilm rule import'), 1)
                self.assertEqual(calls.count('mirror --'), 4)
                self.assertEqual(calls.count('cleanup'), 1)
                self.assertEqual(calls.count('sleep'), 1)
                self.assertEqual(final, drift)
                self.assertEqual(success, '2026-11-12T10:00:00Z\n')

    def test_invalid_retention_never_contacts_mc(self):
        for days in [0, 8, -1, 'bad']:
            result, calls, success, _ = self.run_mirror(days=days)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(calls, '')
            self.assertEqual(success, '')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--verify-primary-lifecycle':
        verify_primary_contract(Path(sys.argv[2]))
    else:
        unittest.main()
