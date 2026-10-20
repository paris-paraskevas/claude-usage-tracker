"""Shared test setup: the repo on sys.path, and a throwaway data dir for the whole session so no
test writes into the real %LOCALAPPDATA%\\ClaudeUsageTracker."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="ctu-tests-")

import pytest  # noqa: E402


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Rebind every data path (and the Claude files) under tmp_path for one test."""
    from ctu import paths, remote
    old = paths.DATA_DIR
    paths.use_data_dir(tmp_path / "data")
    for name, rel in (("PROJECTS_DIR", "projects"), ("CLAUDE_JSON", ".claude.json"),
                      ("CREDS_PATH", "creds.json"), ("CLAUDE_SETTINGS", "settings.json")):
        monkeypatch.setattr(paths, name, tmp_path / rel)
    monkeypatch.setattr(remote, "_remote_id_cache", None)
    yield tmp_path
    paths.use_data_dir(old)


@pytest.fixture
def make_engine(data_dir):
    """Engine factory: no network, no scheduler threads; config defaults plus overrides."""
    import copy

    from ctu import config
    from ctu.engine import Engine
    made = []

    def make(**overrides):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg.update(overrides)
        e = Engine(cfg=cfg, network=False)
        made.append(e)
        return e
    yield make
    for e in made:
        e.stopping = True
        e.scheduler.stop()


def feed(engine, lid, samples, resets_at, src="statusline"):
    """Push readings [(t, pct)] for one limit, then recompute."""
    with engine.lock:
        for t, v in samples:
            engine._reading(lid, v, resets_at, t, src)
        engine.recompute()
