"""Acceptance suite for wordfreq. Runs in the agent's workspace (cwd)."""
import subprocess
import sys
from pathlib import Path

failures = []


def run(*args):
    return subprocess.run([sys.executable, "wordfreq.py", *args],
                          capture_output=True, text=True, timeout=30)


if not Path("wordfreq.py").exists():
    print("wordfreq.py was not created")
    sys.exit(1)

p = run("sample.txt", "3")
if p.stdout.split() != ["the", "6", "fox", "4", "dog", "3"]:
    failures.append(f"top-3: got {p.stdout!r}")

p = run("sample.txt", "5")
if p.stdout.split() != ["the", "6", "fox", "4", "dog", "3", "a", "2", "quick", "2"]:
    failures.append(f"top-5 tiebreak: got {p.stdout!r}")

p = run("sample.txt", "100")
if len(p.stdout.strip().splitlines()) != 17:
    failures.append(f"n>distinct: expected 17 lines, got {len(p.stdout.strip().splitlines())}")

p = run("nope.txt", "3")
if p.returncode != 2 or "error: file not found" not in p.stderr:
    failures.append(f"missing file: exit {p.returncode}, stderr {p.stderr!r}")

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("all acceptance criteria met")
