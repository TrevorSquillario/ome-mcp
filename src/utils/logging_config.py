import os
import sys
import logging


def configure_logging() -> None:
    """Configure the root logger based on the `LOG_LEVEL` env var.

    Routes logs to stderr so they don't corrupt the MCP stdout channel.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"

    # CRITICAL: Stream MUST be sys.stderr for MCP servers
    try:
        logging.basicConfig(stream=sys.stderr, level=level, format=fmt, force=True)
        return
    except TypeError:
        root = logging.getLogger()
        for h in list(root.handlers):
            try:
                root.removeHandler(h)
            except Exception:
                pass
        # CRITICAL: Stream MUST be sys.stderr here too
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(fmt))
        root.setLevel(level)
        root.addHandler(handler)