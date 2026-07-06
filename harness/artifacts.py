"""Run artifacts: manifest, transcript, results, and the runs/index.jsonl index.

Every run directory is self-contained: you can look at it a year later and
know exactly which harness, test version, model, and parameters produced it.
"""

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import HARNESS_VERSION

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "runs"


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git_sha():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return None
        sha = out.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return (sha + ("-dirty" if dirty else "")) if sha else None
    except Exception:
        return None


def hash_test_dir(test_dir: Path) -> str:
    """Content hash of the test definition so a run pins the exact spec."""
    h = hashlib.sha256()
    for f in sorted(test_dir.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(test_dir)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


def new_run_dir(test_name: str, version: str, model_name: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS_DIR / f"{test_name}@{version}" / f"{stamp}__{model_name}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def write_json(path: Path, obj):
    path.write_text(json.dumps(obj, indent=2, default=str) + "\n")


def append_index(entry: dict):
    RUNS_DIR.mkdir(exist_ok=True)
    with open(RUNS_DIR / "index.jsonl", "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def read_index():
    idx = RUNS_DIR / "index.jsonl"
    if not idx.exists():
        return []
    return [json.loads(line) for line in idx.read_text().splitlines() if line.strip()]


def base_manifest(test_name, version, test_dir, model_cfg, driver, trials):
    return {
        "run_id": None,  # filled in with run dir name
        "started_at": utcnow(),
        "finished_at": None,
        "harness": {"version": HARNESS_VERSION, "git_sha": git_sha()},
        "test": {
            "name": test_name,
            "version": version,
            "path": str(test_dir.relative_to(REPO_ROOT)),
            "sha256": hash_test_dir(test_dir),
        },
        "model": model_cfg,
        "driver": driver,
        "trials": trials,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0},
        "result": None,
    }
