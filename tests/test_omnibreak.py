"""Circuit breaker CLI contract — structural checks against an isolated temp
DB (never the real omnibreak.db), since bin/omnibreak.py exposes no public
Python API to record a failure directly; only init/status/reset/stats/list."""
import json
import subprocess
import sys
from pathlib import Path

OMNIBREAK = Path(__file__).resolve().parent.parent / "bin" / "omnibreak"


def run_omnibreak(*args, db_path):
    proc = subprocess.run(
        [sys.executable, str(OMNIBREAK), "--db", str(db_path), *args],
        capture_output=True, text=True, timeout=10,
    )
    return proc


def test_init_creates_db(tmp_path):
    db = tmp_path / "test_omnibreak.db"
    proc = run_omnibreak("init", db_path=db)
    assert proc.returncode == 0
    assert db.exists()
    out = json.loads(proc.stdout)
    assert out["ok"] is True


def test_stats_on_fresh_db_is_empty(tmp_path):
    db = tmp_path / "test_omnibreak.db"
    run_omnibreak("init", db_path=db)
    proc = run_omnibreak("stats", db_path=db)
    out = json.loads(proc.stdout)
    assert out["total_tracked"] == 0
    assert out["blocked"] == 0


def test_reset_all_on_empty_db_does_not_error(tmp_path):
    db = tmp_path / "test_omnibreak.db"
    run_omnibreak("init", db_path=db)
    proc = run_omnibreak("reset-all", db_path=db)
    assert proc.returncode == 0
