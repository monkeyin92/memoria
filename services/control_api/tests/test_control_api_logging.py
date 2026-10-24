from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
_TARGET_MESSAGE = "child layer retention kind=dialogue_summary status=ok subjects=1"
_OTHER_MESSAGE = "unrelated INFO must not reach stderr"

_PROBE = f"""
import copy
import logging
import logging.config
import sys

import uvicorn.config

from services.control_api.app.logging_config import configure_logging
from services.memory_scope import child_layer_retention

logging.config.dictConfig(copy.deepcopy(uvicorn.config.LOGGING_CONFIG))
root = logging.getLogger()
root_level = root.level
root_handlers = tuple(root.handlers)

configure_logging()
configure_logging()

assert root.level == root_level, (root.level, root_level)
assert tuple(root.handlers) == root_handlers
target = logging.getLogger("services.memory_scope.child_layer_retention")
assert target is child_layer_retention.logger
assert target.level == logging.INFO, target.level
assert target.propagate is False
assert len(target.handlers) == 1, target.handlers
handler = target.handlers[0]
assert isinstance(handler, logging.StreamHandler)
assert handler.stream is sys.stderr

logging.getLogger("services.other_module").info({_OTHER_MESSAGE!r})
target.info({_TARGET_MESSAGE!r})
"""


@pytest.mark.hermetic
def test_only_child_retention_logger_gets_one_stderr_handler() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr.count(_TARGET_MESSAGE) == 1
    assert _OTHER_MESSAGE not in completed.stderr
