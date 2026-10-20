"""Command line. The hook / statusline / toast-action fast paths are dispatched before argparse
and the rest of the app is imported, so they stay cheap on every Claude Code turn."""
from __future__ import annotations

import sys

INSTANCE_PORT = 49222     # held for the process lifetime: one tray per user session


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--session-hook" in argv:
        from ctu.hookclient import run_session_hook
        return run_session_hook()
    if "--statusline" in argv:
        from ctu.statusline import run
        return run()
    if "--action" in argv:
        from ctu.hookclient import run_action
        i = argv.index("--action")
        return run_action(argv[i + 1] if i + 1 < len(argv) else "")
    return _main(argv)


def _main(argv) -> int:
    import argparse

    from ctu import APP_NAME, __version__
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="claude-usage-tracker", description=APP_NAME)
    ap.add_argument("--once", action="store_true", help="print usage once and exit")
    ap.add_argument("--debug", action="store_true", help="with --once, dump the raw API JSON")
    ap.add_argument("--selftest", action="store_true", help="build state from fixtures (no network) and exit")
    ap.add_argument("--headless", action="store_true", help="run the engine + local API only (no tray / windows)")
    ap.add_argument("--port", type=int, default=None, help="local API port (default from config, 8787)")
    ap.add_argument("--install", action="store_true", help="interactive setup (shortcuts + autostart)")
    ap.add_argument("--uninstall", action="store_true", help="remove shortcuts and our Claude Code hooks / status line")
    ap.add_argument("--remove-hook", action="store_true",
                    help="remove only our hooks and status line from ~/.claude/settings.json (used by the uninstaller)")
    ap.add_argument("--install-autostart", action="store_true", help="add the Startup shortcut only")
    ap.add_argument("--uninstall-autostart", action="store_true")
    ap.add_argument("--wait-for-lock", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--session-hook", action="store_true", help="(internal) Claude Code hook client")
    ap.add_argument("--statusline", action="store_true", help="(internal) Claude Code statusLine command")
    ap.add_argument("--action", default=None, help="(internal) toast button URI")
    args = ap.parse_args(argv)

    if args.version:
        print(f"{APP_NAME} {__version__}")
        return 0
    if args.selftest:
        from ctu.selftest import run
        return run()
    if args.install:
        from ctu.install import do_install
        do_install()
        return 0
    if args.uninstall:
        from ctu.install import do_uninstall
        do_uninstall()
        return 0
    if args.remove_hook:
        from ctu.install import remove_claude_integration
        return 0 if remove_claude_integration() else 1
    if args.install_autostart:
        from ctu.install import install_autostart
        install_autostart()
        return 0
    if args.uninstall_autostart:
        from ctu.install import uninstall_autostart
        uninstall_autostart()
        return 0
    if args.once:
        return run_once(args.debug)
    return run_app(args.port, args.headless, args.wait_for_lock)


def run_once(debug: bool) -> int:
    import json

    from ctu import config, forecast, usage
    cfg = config.INTERNAL
    r = usage.fetch_usage(int(cfg.get("request_timeout_seconds", 20)))
    if r.ok:
        parts = [f"{forecast.LABELS[k]} {w['pct']:.0f}%->{forecast.fmt_clock(w['resets_at'])}"
                 for k, w in r.windows.items()]
        print("STATUS:", "  ".join(parts) or "no data")
        for key, w in r.windows.items():
            print(f"  {key:16s} {w['pct']:6.1f}%   resets {forecast.fmt_clock(w['resets_at'])}")
        e = r.extra
        if e and e.get("enabled") and e.get("used") is not None:
            print(f"  extra usage      {forecast.fmt_money(e['used'], e['currency'])} / "
                  f"{forecast.fmt_money(e['limit'], e['currency'])}  ({e['pct']:.0f}%)")
    else:
        print("ERROR:", r.error, f"(token_state={r.token_state})")
    if debug:
        print("\n--- RAW RESPONSE ---")
        print(json.dumps(r.raw, indent=2)[:6000] if r.raw is not None else "(no body)")
    return 0 if r.ok else 1


def _bind_guard(port: int):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    except OSError:
        pass
    s.bind(("127.0.0.1", port))
    return s


def _acquire_instance(wait: bool):
    import time
    deadline = time.time() + (20 if wait else 0)
    while True:
        try:
            return _bind_guard(INSTANCE_PORT)
        except OSError:
            if time.time() >= deadline:
                return None
            time.sleep(0.5)


def _protocol_command() -> str:
    import os
    from pathlib import Path
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --action "%1"'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    script = Path(__file__).resolve().parent.parent / "claude_usage_tracker.py"
    target = f'"{script}"' if script.exists() else "-m ctu"
    return f'"{pyw if pyw.exists() else exe}" {target} --action "%1"' if os.name == "nt" else ""


def run_app(port, headless: bool, wait_for_lock: bool) -> int:
    import threading

    from ctu import io, paths, winplat
    guard = _acquire_instance(wait_for_lock)
    if guard is None:
        from ctu.hookclient import post
        post("/api/v2/actions/open_dashboard", {"view": "now"})   # already running: bring it forward
        io.log("another instance is running; asked it to open the dashboard")
        return 0
    paths.ensure_data_dir()
    winplat.set_dpi_awareness()
    winplat.set_app_user_model_id()
    from ctu import server
    from ctu.engine import Engine
    engine = Engine()
    srv = server.start(engine, port or int(engine.cfg.get("dashboard_port", 8787)))
    engine.start()
    io.log(f"tracker {__import__('ctu').__version__} started")
    if winplat.IS_WIN:
        cmd = _protocol_command()
        if cmd:
            threading.Thread(target=winplat.register_protocol, args=(cmd,), daemon=True).start()
    try:
        if headless:
            _wait_forever(engine)
        else:
            _run_desktop(engine)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        if srv:
            srv.shutdown()
        io.log("tracker stopped")
        guard.close()
    return 0


def _wait_forever(engine) -> None:
    import threading

    from ctu import windows
    stop = threading.Event()
    engine.host = windows.BrowserHost(engine, on_quit=stop.set)
    while not stop.wait(1.0):
        pass


def _run_desktop(engine) -> None:
    import threading

    from ctu import io, windows
    from ctu.tray import TrayHost
    try:
        import pystray  # noqa: F401
    except Exception as exc:
        io.log(f"no tray available ({exc}); running headless")
        print(f"Running headless (no tray: {exc}). Dashboard: http://127.0.0.1:{engine.port}/ — Ctrl+C to stop.")
        _wait_forever(engine)
        return
    if windows.available():
        shell = windows.Shell(engine)
        tray = TrayHost(engine, shell)
        shell.tray = tray
        engine.host = shell
        threading.Thread(target=tray.run, name="ctu-tray", daemon=True).start()
        shell.run()                  # main thread, until quit
    else:
        host = windows.BrowserHost(engine)
        tray = TrayHost(engine, None)
        host.tray = tray
        engine.host = host
        tray.run()
