"""Anthropic service status (Statuspage v2 summary)."""
from __future__ import annotations

import json
import urllib.request

STATUS_URL = "https://status.anthropic.com/api/v2/summary.json"
INDICATORS = ("none", "minor", "major", "critical")


def fetch_status(timeout: int = 10):
    """{indicator, description, components:[{name, status}], url} or None."""
    try:
        req = urllib.request.Request(STATUS_URL, headers={"User-Agent": "claude-usage-tracker",
                                                          "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.load(resp)
        st = d.get("status", {}) or {}
        comps = [{"name": c.get("name", ""), "status": c.get("status", "operational")}
                 for c in d.get("components", []) if not c.get("group")]
        return {"indicator": st.get("indicator", "none"), "description": st.get("description", ""),
                "components": comps, "url": (d.get("page", {}) or {}).get("url", "https://status.anthropic.com")}
    except Exception:
        return None


def service_view(raw, watch: list | None = None) -> dict:
    """state.service. With watched components, the indicator reflects only those."""
    if not isinstance(raw, dict):
        return {"indicator": "unknown", "description": "Status unavailable", "url": "https://status.anthropic.com",
                "affected": []}
    comps = raw.get("components") or []
    affected = [c["name"] for c in comps if c.get("status") not in (None, "operational")]
    indicator = raw.get("indicator") if raw.get("indicator") in INDICATORS else "unknown"
    if watch:
        affected = [n for n in affected if n in watch]
        worst = 0
        rank = {"operational": 0, "under_maintenance": 1, "degraded_performance": 1,
                "partial_outage": 2, "major_outage": 3}
        for c in comps:
            if c.get("name") in watch:
                worst = max(worst, rank.get(c.get("status"), 0))
        indicator = INDICATORS[worst]
    return {"indicator": indicator, "description": raw.get("description") or "",
            "url": raw.get("url") or "https://status.anthropic.com", "affected": affected}
