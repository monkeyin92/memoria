"""Bounded authorized acoustic test; restore both prior output volume and mute."""
import argparse
import importlib.util
import json
import re
import signal
import subprocess
import sys
from pathlib import Path


def applescript(script):
    return subprocess.check_output(['osascript', '-e', script], text=True, timeout=5).strip()


def stopped(signum, frame):
    raise KeyboardInterrupt(f'stop signal {signum}')


def read_settings():
    raw = applescript('get volume settings')
    volume = re.search(r'output volume:(\d+)', raw)
    muted = re.search(r'output muted:(true|false)', raw)
    if not volume or not muted or not 0 <= int(volume[1]) <= 100:
        raise RuntimeError('could not determine prior output volume and mute state')
    return {'output_volume': int(volume[1]), 'output_muted': muted[1], 'raw': raw}


def run_session(argv):
    spec = importlib.util.spec_from_file_location(
        'auto_audio_session', Path(__file__).with_name('auto_audio_session.py')
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.main(argv)


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--output-volume', type=int, default=None)
    args, forwarded = parser.parse_known_args(argv)
    if args.output_volume is not None and not 1 <= args.output_volume <= 100:
        raise ValueError('--output-volume must be 1..100')
    if '--run' not in forwarded or '--dry-run' in forwarded:
        return run_session(forwarded)
    prior = read_settings()
    receipt = {'prior_volume_settings': prior}
    print(json.dumps(receipt), flush=True)
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for sig in handlers:
            signal.signal(sig, stopped)
        if args.output_volume is not None:
            applescript(f'set volume output volume {args.output_volume}')
        applescript('set volume output muted false')
        active = read_settings()
        if active['output_muted'] != 'false' or (
            args.output_volume is not None and active['output_volume'] != args.output_volume
        ):
            raise RuntimeError('requested test output settings did not verify')
        receipt['test_volume_settings'] = active
        print(json.dumps({'test_volume_settings': active}), flush=True)
        return run_session(forwarded)
    finally:
        for sig in handlers:
            signal.signal(sig, signal.SIG_IGN)
        errors = []
        # A failure restoring volume must not skip restoring mute.
        for command in (f"set volume output volume {prior['output_volume']}",
                        f"set volume output muted {prior['output_muted']}"):
            try:
                applescript(command)
            except Exception as error:
                errors.append(f'{type(error).__name__}: {error}')
        try:
            final = read_settings()
            receipt['final_volume_settings'] = final
            receipt['volume_restored'] = final['output_volume'] == prior['output_volume']
            receipt['mute_restored'] = final['output_muted'] == prior['output_muted']
        except Exception as error:
            errors.append(f'{type(error).__name__}: {error}')
            receipt['volume_restored'] = receipt['mute_restored'] = False
        receipt['restoration_errors'] = errors
        print(json.dumps(receipt), flush=True)
        try:
            if '--run-dir' in forwarded:
                directory = Path(forwarded[forwarded.index('--run-dir') + 1])
                if directory.is_dir():
                    (directory / 'volume-restore.json').write_text(
                        json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
                    )
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        if errors or not receipt['volume_restored'] or not receipt['mute_restored']:
            raise RuntimeError('audio restoration did not verify')


if __name__ == '__main__':
    raise SystemExit(main())
