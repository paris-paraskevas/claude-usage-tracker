"""Claude Usage Tracker — entry point. The engine lives in the `ctu` package, the UI in ctu/ui.

    pythonw claude_usage_tracker.py             # tray app (normal use)
    python  claude_usage_tracker.py --once      # print usage once
    python  claude_usage_tracker.py --selftest  # build state from fixtures, no network
"""
import sys

from ctu import __version__  # noqa: F401  (console entry points / CI read it)


def main(argv=None) -> int:
    from ctu.cli import main as _main
    return _main(argv)


if __name__ == "__main__":
    sys.exit(main())
