"""Hub-owned logging: INFO by default, opt-in DEBUG without third-party payloads.

Only callers' explicitly selected fields belong in this logger. Never log request
headers, URLs, SQL/parameters, subprocess output, exceptions, or job contents.
"""

from __future__ import annotations

import logging
import os
import sys


def configure_logging() -> None:
    """Configure the Hub namespace once in API or worker processes."""
    level = logging.DEBUG if os.environ.get("PALIMPSEST_HUB_LOG_LEVEL", "INFO").upper() == "DEBUG" else logging.INFO
    hub_logger = logging.getLogger("palimpsest_hub")
    if not any(getattr(handler, "_palimpsest_hub", False) for handler in hub_logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        handler._palimpsest_hub = True
        hub_logger.addHandler(handler)
    hub_logger.setLevel(level)
    hub_logger.propagate = False
    # Uvicorn's access logger prints the raw request target including query values.
    logging.getLogger("uvicorn.access").disabled = True
