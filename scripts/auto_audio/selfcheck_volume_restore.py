"""Offline checks: never touches speakers, microphone, or system settings."""
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Same bootstrap as the sibling self-checks: this module has to import as a script *and* from
# pytest (`tests/test_auto_audio_tool.py`), where its sibling is not on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_with_volume_restore as wrapper  # noqa: E402


class VolumeRestoreTest(unittest.TestCase):
    def simulate(self, *, muted='true', driver_error=None, fail_command=None, dry=False):
        state = {'volume': 31, 'muted': muted}
        commands = []
        failed = False

        def script(command):
            nonlocal failed
            commands.append(command)
            if command == fail_command and not failed:
                failed = True
                raise OSError('simulated setting error')
            if command == 'get volume settings':
                return (f"output volume:{state['volume']}, input volume:82, "
                        f"alert volume:100, output muted:{state['muted']}")
            if command.startswith('set volume output volume '):
                state['volume'] = int(command.split()[-1])
            elif command.startswith('set volume output muted '):
                state['muted'] = command.split()[-1]
            return ''

        def run(argv):
            self.assertNotIn('--output-volume', argv)
            if not dry:
                self.assertEqual(state, {'volume': 80, 'muted': 'false'})
            if driver_error:
                raise driver_error
            return 3

        with patch.object(wrapper, 'applescript', side_effect=script), \
             patch.object(wrapper, 'run_session', side_effect=run), \
             contextlib.redirect_stdout(io.StringIO()):
            error = None
            try:
                result = wrapper.main(['--dry-run' if dry else '--run', '--output-volume', '80'])
            except BaseException as caught:
                error = caught
                result = None
        return state, commands, result, error

    def test_session_status_restores(self):
        for muted in ('true', 'false'):
            with self.subTest(muted=muted):
                state, _, result, error = self.simulate(muted=muted)
                self.assertEqual(state, {'volume': 31, 'muted': muted})
                self.assertEqual(result, 3)
                self.assertIsNone(error)

    def test_exception_and_interrupt_restore(self):
        for failure in (RuntimeError('driver failure'), KeyboardInterrupt('interrupted')):
            with self.subTest(failure=failure):
                state, _, _, error = self.simulate(driver_error=failure)
                self.assertEqual(state, {'volume': 31, 'muted': 'true'})
                self.assertIs(error, failure)

    def test_partial_setup_failure_restores(self):
        state, _, _, error = self.simulate(fail_command='set volume output muted false')
        self.assertEqual(state, {'volume': 31, 'muted': 'true'})
        self.assertIsInstance(error, OSError)

    def test_restore_error_still_restores_mute_and_fails(self):
        state, commands, _, error = self.simulate(fail_command='set volume output volume 31')
        self.assertEqual(state['muted'], 'true')
        self.assertIn('set volume output muted true', commands)
        self.assertIsInstance(error, RuntimeError)

    def test_dry_run_never_touches_settings(self):
        state, commands, result, error = self.simulate(dry=True)
        self.assertEqual(commands, [])
        self.assertEqual(state, {'volume': 31, 'muted': 'true'})
        self.assertEqual(result, 3)
        self.assertIsNone(error)


if __name__ == '__main__':
    unittest.main()
