"""llm-evals runner.

Usage:
  llm-evals list
  llm-evals run <test> --model models/foo.yaml [--version 2] [--trials 3] [--judge models/judge.yaml]
  llm-evals report [--test NAME] [--model NAME]

A <test> is a name under tests/ (latest version by default) or a direct path
to a tests/<name>/v<N> directory.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml
from openai import OpenAI

from . import artifacts
from .checks import extract_json, run_checks, score

TESTS_DIR = artifacts.REPO_ROOT / "tests"
MAX_TOOL_OUTPUT = 8000

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command in the task workspace. Returns stdout and stderr.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Shell command to run"}},
            "required": ["command"],
        },
    },
}


# ---------------------------------------------------------------- resolution

def resolve_test(ref: str, version: str | None):
    path = Path(ref)
    if path.is_dir() and (path / "test.yaml").exists():
        test_dir = path.resolve()
        name, ver = test_dir.parent.name, test_dir.name
    else:
        base = TESTS_DIR / ref
        if not base.is_dir():
            sys.exit(f"error: no test named {ref!r} under tests/")
        versions = sorted((d.name for d in base.iterdir() if d.is_dir() and d.name.startswith("v")),
                          key=lambda v: int(v[1:]))
        if not versions:
            sys.exit(f"error: {ref!r} has no v* version directories")
        ver = f"v{version}" if version else versions[-1]
        test_dir, name = base / ver, ref
        if not (test_dir / "test.yaml").exists():
            sys.exit(f"error: {test_dir} has no test.yaml")
    spec = yaml.safe_load((test_dir / "test.yaml").read_text())
    return name, ver, test_dir, spec


def load_model(path: str):
    cfg = yaml.safe_load(Path(path).read_text())
    cfg.setdefault("name", Path(path).stem)
    cfg.setdefault("params", {})
    return cfg


def make_client(cfg):
    import os
    key = os.environ.get(cfg.get("api_key_env", ""), cfg.get("api_key", "none"))
    return OpenAI(base_url=cfg["base_url"], api_key=key)


# ------------------------------------------------------------------ drivers

def track_usage(manifest, resp):
    u = getattr(resp, "usage", None)
    manifest["usage"]["requests"] += 1
    if u:
        manifest["usage"]["prompt_tokens"] += u.prompt_tokens or 0
        manifest["usage"]["completion_tokens"] += u.completion_tokens or 0


def drive_single(client, model_cfg, spec, manifest, workspace):
    messages = []
    if spec.get("system"):
        messages.append({"role": "system", "content": spec["system"]})
    messages.append({"role": "user", "content": spec["prompt"]})
    params = {**model_cfg["params"], **spec.get("params", {})}
    resp = client.chat.completions.create(model=model_cfg["model"], messages=messages, **params)
    track_usage(manifest, resp)
    text = resp.choices[0].message.content or ""
    messages.append({"role": "assistant", "content": text})
    return text, messages


def drive_toolloop(client, model_cfg, spec, manifest, workspace, test_dir):
    """Agentic loop: the model gets a bash tool in an isolated workspace and
    works the spec until it stops calling tools or hits its budget.

    NOTE: commands run as your user with cwd=workspace — fine for your own
    tasks, not for untrusted ones. Wrap in a container if that changes.
    """
    agent_cfg = spec.get("agent", {})
    max_steps = agent_cfg.get("max_steps", 30)
    max_seconds = agent_cfg.get("max_seconds", 900)
    prompt = (test_dir / spec["spec"]).read_text() if spec.get("spec") else spec["prompt"]

    system = agent_cfg.get("system") or (
        "You are an autonomous engineer working alone in a Linux workspace; no one will "
        "answer questions. Use the bash tool to inspect files, write code, and verify your "
        "work by actually running it. Your work is graded only by what exists in the "
        "workspace when you finish — check the spec's acceptance criteria yourself before "
        "stopping. When everything passes, reply with a short summary and no tool calls."
    )
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": prompt}]
    params = {**model_cfg["params"], **spec.get("params", {})}
    t0 = time.time()

    for _ in range(max_steps):
        if time.time() - t0 > max_seconds:
            messages.append({"role": "user", "content": "[harness] time budget exhausted"})
            break
        resp = client.chat.completions.create(
            model=model_cfg["model"], messages=messages, tools=[BASH_TOOL], **params)
        track_usage(manifest, resp)
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            break
        for tc in msg.tool_calls:
            try:
                cmd = json.loads(tc.function.arguments).get("command", "")
                proc = subprocess.run(cmd, shell=True, cwd=workspace,
                                      capture_output=True, text=True, timeout=120)
                out = (proc.stdout + proc.stderr) or f"(no output, exit {proc.returncode})"
                if proc.returncode != 0:
                    out += f"\n[exit code {proc.returncode}]"
            except subprocess.TimeoutExpired:
                out = "[command timed out after 120s]"
            except Exception as e:
                out = f"[harness error running command: {e}]"
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": out[-MAX_TOOL_OUTPUT:]})

    final = next((m.get("content") or "" for m in reversed(messages)
                  if m.get("role") == "assistant"), "")
    return final, messages


def drive_command(client, model_cfg, spec, manifest, workspace, test_dir):
    """Run an external agent CLI (claude -p, codex exec, aider, ...) against the
    workspace, terminal-bench style. Placeholders: {workspace} {spec} {model} {base_url}.
    """
    agent_cfg = spec["agent"]
    spec_path = test_dir / spec["spec"] if spec.get("spec") else None
    cmd = agent_cfg["command"].format(
        workspace=workspace, spec=spec_path,
        model=model_cfg["model"], base_url=model_cfg.get("base_url", ""))
    proc = subprocess.run(cmd, shell=True, cwd=workspace, capture_output=True,
                          text=True, timeout=agent_cfg.get("max_seconds", 1800))
    transcript = [{"role": "harness", "content": f"$ {cmd}"},
                  {"role": "agent_stdout", "content": proc.stdout[-100_000:]},
                  {"role": "agent_stderr", "content": proc.stderr[-20_000:]}]
    return proc.stdout, transcript


# ------------------------------------------------------------------- judge

def make_judge(judge_path):
    if not judge_path:
        return None
    cfg = load_model(judge_path)
    client = make_client(cfg)

    def judge(rubric, output):
        resp = client.chat.completions.create(
            model=cfg["model"],
            messages=[{"role": "user", "content":
                       "You are grading an AI model's output against a rubric. Be strict.\n\n"
                       f"RUBRIC:\n{rubric}\n\nOUTPUT TO GRADE:\n{output}\n\n"
                       'Respond with only a JSON object: {"pass": true|false, "reason": "..."}'}],
            temperature=0)
        return extract_json(resp.choices[0].message.content or "")
    return judge


# --------------------------------------------------------------------- run

def run_trial(client, model_cfg, spec, manifest, test_dir, run_dir, trial_n, judge):
    workspace = run_dir / f"trial{trial_n}" / "workspace"
    workspace.mkdir(parents=True)
    if (test_dir / "files").is_dir():
        shutil.copytree(test_dir / "files", workspace, dirs_exist_ok=True)

    driver = spec.get("type", "single")
    if driver == "single":
        output, transcript = drive_single(client, model_cfg, spec, manifest, workspace)
    elif driver == "agentic":
        if spec.get("agent", {}).get("command"):
            output, transcript = drive_command(client, model_cfg, spec, manifest, workspace, test_dir)
        else:
            output, transcript = drive_toolloop(client, model_cfg, spec, manifest, workspace, test_dir)
    else:
        sys.exit(f"error: unknown test type {driver!r}")

    artifacts.write_json(run_dir / f"trial{trial_n}" / "transcript.json", transcript)
    ctx = {"test_dir": test_dir, "workspace": workspace, "judge": judge}
    check_results = run_checks(spec.get("checks", []), output, ctx)
    trial_score = score(check_results)
    min_score = spec.get("acceptance", {}).get("min_score", 1.0)
    return {"trial": trial_n, "score": round(trial_score, 4),
            "passed": trial_score >= min_score, "checks": check_results}


def cmd_run(args):
    name, ver, test_dir, spec = resolve_test(args.test, args.version)
    model_cfg = load_model(args.model)
    client = make_client(model_cfg)
    judge = make_judge(args.judge)
    trials = args.trials or spec.get("trials", 1)
    driver = spec.get("type", "single")

    run_dir = artifacts.new_run_dir(name, ver, model_cfg["name"])
    manifest = artifacts.base_manifest(name, ver, test_dir, model_cfg, driver, trials)
    manifest["run_id"] = run_dir.name
    print(f"▶ {name}@{ver} [{driver}] on {model_cfg['name']} — {trials} trial(s)")
    print(f"  run dir: {run_dir.relative_to(artifacts.REPO_ROOT)}")

    trial_results = []
    for n in range(1, trials + 1):
        result = run_trial(client, model_cfg, spec, manifest, test_dir, run_dir, n, judge)
        trial_results.append(result)
        mark = "PASS" if result["passed"] else "FAIL"
        print(f"  trial {n}: {mark} (score {result['score']})")
        for c in result["checks"]:
            print(f"    {'✓' if c['passed'] else '✗'} {c['name']}: {c['detail'][:120]}")

    min_pass_rate = spec.get("acceptance", {}).get("min_pass_rate", 1.0)
    pass_rate = sum(t["passed"] for t in trial_results) / len(trial_results)
    manifest["result"] = {
        "passed": pass_rate >= min_pass_rate,
        "pass_rate": round(pass_rate, 4),
        "mean_score": round(sum(t["score"] for t in trial_results) / len(trial_results), 4),
        "trials": trial_results,
    }
    manifest["finished_at"] = artifacts.utcnow()
    artifacts.write_json(run_dir / "manifest.json", manifest)
    artifacts.append_index({
        "run_id": manifest["run_id"], "test": name, "version": ver,
        "test_sha": manifest["test"]["sha256"], "model": model_cfg["name"],
        "driver": driver, "started_at": manifest["started_at"],
        "passed": manifest["result"]["passed"],
        "pass_rate": manifest["result"]["pass_rate"],
        "mean_score": manifest["result"]["mean_score"],
        "run_dir": str(run_dir.relative_to(artifacts.REPO_ROOT)),
    })
    verdict = "PASSED" if manifest["result"]["passed"] else "FAILED"
    print(f"■ {verdict} — pass rate {pass_rate:.0%}, mean score {manifest['result']['mean_score']}")
    return 0 if manifest["result"]["passed"] else 1


def cmd_list(args):
    for base in sorted(TESTS_DIR.iterdir()) if TESTS_DIR.is_dir() else []:
        if not base.is_dir():
            continue
        for vdir in sorted(base.iterdir()):
            tf = vdir / "test.yaml"
            if tf.exists():
                spec = yaml.safe_load(tf.read_text())
                print(f"{base.name}@{vdir.name:<4} [{spec.get('type', 'single'):<7}] "
                      f"{spec.get('description', '')}")
    return 0


def cmd_report(args):
    rows = artifacts.read_index()
    if args.test:
        rows = [r for r in rows if r["test"] == args.test]
    if args.model:
        rows = [r for r in rows if r["model"] == args.model]
    if not rows:
        print("no runs recorded")
        return 0
    groups = {}
    for r in rows:
        groups.setdefault((r["test"], r["version"], r["model"]), []).append(r)
    print(f"{'test':<28} {'ver':<4} {'model':<24} {'runs':>4} {'pass':>6} {'score':>6}  last run")
    for (test, ver, model), rs in sorted(groups.items()):
        rs.sort(key=lambda r: r["started_at"])
        pass_rate = sum(r["passed"] for r in rs) / len(rs)
        mean = sum(r["mean_score"] for r in rs) / len(rs)
        print(f"{test:<28} {ver:<4} {model:<24} {len(rs):>4} {pass_rate:>6.0%} "
              f"{mean:>6.2f}  {rs[-1]['started_at']}")
    return 0


def main():
    p = argparse.ArgumentParser(prog="llm-evals", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="run a test against a model")
    pr.add_argument("test", help="test name (tests/<name>) or path to a version dir")
    pr.add_argument("--model", required=True, help="path to a models/*.yaml config")
    pr.add_argument("--version", help="test version number (default: latest)")
    pr.add_argument("--trials", type=int, help="override trial count")
    pr.add_argument("--judge", help="models/*.yaml config for judge checks")
    pr.set_defaults(fn=cmd_run)

    pl = sub.add_parser("list", help="list tests and versions")
    pl.set_defaults(fn=cmd_list)

    pp = sub.add_parser("report", help="summarize recorded runs")
    pp.add_argument("--test")
    pp.add_argument("--model")
    pp.set_defaults(fn=cmd_report)

    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
