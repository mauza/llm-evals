"""Built-in acceptance checks.

Each check spec is a dict from test.yaml with a `type` plus type-specific
fields, and optional `name` and `weight`. Every check returns
{"name", "type", "weight", "passed", "detail"}.
"""

import json
import re
import subprocess
import sys


def extract_json(text):
    """Pull the first JSON object out of a response, tolerating code fences."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    raise ValueError("no parseable JSON object in output")


def _contains(spec, output, ctx):
    needle, hay = spec["value"], output
    if not spec.get("case_sensitive", False):
        needle, hay = needle.lower(), hay.lower()
    found = needle in hay
    return found, f"{needle!r} {'found' if found else 'not found'}"


def _not_contains(spec, output, ctx):
    found, _ = _contains(spec, output, ctx)
    return not found, f"{spec['value']!r} {'absent' if not found else 'present'}"


def _regex(spec, output, ctx):
    m = re.search(spec["pattern"], output, re.MULTILINE)
    return bool(m), f"pattern {spec['pattern']!r} {'matched' if m else 'did not match'}"


def _exact(spec, output, ctx):
    ok = output.strip() == spec["value"].strip()
    return ok, "exact match" if ok else f"got {output.strip()[:200]!r}"


def _json_check(spec, output, ctx):
    obj = extract_json(output)
    missing = [k for k in spec.get("require_keys", []) if k not in obj]
    if missing:
        return False, f"missing keys: {missing}"
    for key, expected in spec.get("expect", {}).items():
        if obj.get(key) != expected:
            return False, f"{key}={obj.get(key)!r}, expected {expected!r}"
    return True, f"valid JSON with keys {sorted(obj)}"


def _script(spec, output, ctx):
    """Run a grader script; exit code 0 = pass.

    The script gets the model output (or agent workspace) to inspect:
    argv[1] = path to a file containing the model's final text output,
    cwd     = the workspace directory (for agentic tests, the dir the
              agent worked in). Python scripts run under the harness venv
    so they can import pyyaml etc.
    """
    script = ctx["test_dir"] / spec["path"]
    output_file = ctx["workspace"] / "_final_output.txt"
    output_file.write_text(output)
    cmd = [sys.executable if script.suffix == ".py" else "bash", str(script), str(output_file)]
    proc = subprocess.run(
        cmd, cwd=ctx["workspace"], capture_output=True, text=True,
        timeout=spec.get("timeout", 120),
    )
    detail = (proc.stdout + proc.stderr).strip()[-2000:] or f"exit {proc.returncode}"
    return proc.returncode == 0, detail


def _judge(spec, output, ctx):
    """Grade with a judge model against a rubric. Requires --judge."""
    judge = ctx.get("judge")
    if judge is None:
        return False, "no judge model configured (pass --judge models/<cfg>.yaml)"
    verdict = judge(spec["rubric"], output)
    return verdict["pass"], verdict.get("reason", "")


CHECKS = {
    "contains": _contains,
    "not_contains": _not_contains,
    "regex": _regex,
    "exact": _exact,
    "json": _json_check,
    "script": _script,
    "judge": _judge,
}


def run_checks(checks, output, ctx):
    results = []
    for spec in checks:
        ctype = spec["type"]
        try:
            passed, detail = CHECKS[ctype](spec, output, ctx)
        except Exception as e:  # a broken check is a failed check, not a crashed run
            passed, detail = False, f"check error: {e}"
        results.append({
            "name": spec.get("name", ctype),
            "type": ctype,
            "weight": float(spec.get("weight", 1.0)),
            "passed": bool(passed),
            "detail": detail,
        })
    return results


def score(check_results):
    total = sum(c["weight"] for c in check_results)
    if total == 0:
        return 0.0
    return sum(c["weight"] for c in check_results if c["passed"]) / total
