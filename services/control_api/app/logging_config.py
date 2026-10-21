"""Logging setup for the child layer retention worker.

Only that worker is configured. Giving the root logger a handler would also
emit every other module's INFO records, including records that may carry
child-identifying data.
"""

from __future__ import annotations

import logging
import sys

_TARGET_LOGGER = "services.memory_scope.child_layer_retention"
_HANDLER_NAME = "memoria-child-layer-retention-stderr"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging() -> None:
    """Attach one stderr handler to the child layer retention logger only."""

    logger = logging.getLogger(_TARGET_LOGGER)
    if not any(handler.get_name() == _HANDLER_NAME for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.set_name(_HANDLER_NAME)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


__all__ = ["configure_logging"]
