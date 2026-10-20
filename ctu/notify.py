"""Windows toasts (winotify). Best effort: any failure is logged and swallowed.

Buttons use our `claude-usage-tracker:` URL protocol (registered per user by winplat), which
launches `--action <uri>` -> a POST to the running tray."""
from __future__ import annotations

from ctu import APP_NAME, io, paths, winplat

PROTOCOL = "claude-usage-tracker"


def _ps_safe(s: str) -> str:
    """winotify builds a PowerShell here-string: neutralise `$` and the backtick escape."""
    return str(s or "").replace("`", "'").replace("$", "＄").replace('"@', '" @')[:300]


def toast(title: str, body: str, *, tag: str = "", actions: bool = True) -> bool:
    try:
        from winotify import Notification, audio
    except Exception:
        return False
    try:
        linked = actions and winplat.protocol_registered()
        kwargs = {"app_id": APP_NAME, "title": _ps_safe(title), "msg": _ps_safe(body), "duration": "short"}
        if paths.ICON_PATH.exists():
            kwargs["icon"] = str(paths.ICON_PATH)
        if linked:
            kwargs["launch"] = f"{PROTOCOL}:open/now"       # clicking the toast body
        t = Notification(**kwargs)
        if tag:
            t.tag = tag[:60]                                  # one replaceable toast per alert kind
        if linked:
            t.add_actions("Open dashboard", f"{PROTOCOL}:open/now")
            t.add_actions("Mute 1 h", f"{PROTOCOL}:mute/hour")
        # Focus Assist / Do Not Disturb already route toasts to the Notification Center; while a
        # full-screen app or presentation runs we also stay silent (winotify's default).
        if winplat.notification_state() not in ("busy", "fullscreen", "presentation"):
            t.set_audio(audio.Default, loop=False)
        t.show()
        return True
    except Exception as exc:
        io.log(f"toast failed: {exc}")
        return False
