"""Hub-owned logging: INFO by default, opt-in DEBUG without third-party payloads.

Only callers' explicitly selected fields belong in this logger. Never log request
headers, URLs, SQL/parameters, subprocess output, exception messages, tracebacks,
or job contents. A swallowed failure is identified through ``failure_fields``.
"""

from __future__ import annotations

import logging
import os
import re
import sys

_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,119}\Z")


def failure_fields(exc: BaseException) -> str:
    """Return ``cause=<class> site=<module>:<function>:<line>`` for a swallowed failure.

    Both values come from code objects, never from the exception's arguments:
    its message and traceback text can carry SQL binds, URLs, paths or
    credentials. The site is the innermost Hub frame, so a driver or provider
    failure still names the Hub call that reached it.
    """
    kind = type(exc)
    cause = f"{kind.__module__}.{kind.__name__}"
    if not _CLASS_NAME.match(cause):
        cause = "unknown"
    site = "unknown"
    tb = exc.__traceback__
    while tb is not None:
        module = tb.tb_frame.f_globals.get("__name__")
        if isinstance(module, str) and (module == "palimpsest_hub" or module.startswith("palimpsest_hub.")):
            site = f"{module}:{tb.tb_frame.f_code.co_name}:{tb.tb_lineno}"
        tb = tb.tb_next
    return f"cause={cause} site={site}"


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
