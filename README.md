# llm-evals

Personal, versioned tests/benchmarks for LLMs, with a runner and self-contained
run artifacts. The goal: encode what *I* care about in a model as measurable
tests with acceptance criteria, and get realistic results by letting the model
work a spec autonomously instead of chatting back and forth.

## Layout

```
tests/<name>/v<N>/     one immutable version of one test
  test.yaml            spec: type, prompt/spec, params, trials, acceptance, checks
  spec.md              (agentic) the spec the agent works from
  files/               fixtures copied into the agent workspace
  grade.py|grade.sh    script grader (exit 0 = pass)
models/<name>.yaml     endpoint config (any OpenAI-compatible server)
harness/               the runner
runs/<test>@v<N>/<ts>__<model>/   artifacts: manifest.json, trialN/transcript.json,
                                  trialN/workspace/, and runs/index.jsonl index
```

## Usage

```bash
uv sync
uv run llm-evals list
uv run llm-evals run json-extract --model models/qwen36-27b.yaml
uv run llm-evals run agentic-wordfreq --model models/qwen36-27b.yaml
uv run llm-evals report                      # pass rates by test/version/model
```

Exit code is 0 iff the run met the test's acceptance criteria, so runs can gate
things in CI.

## Test types

- **single** — one prompt, one response, checked deterministically
  (`contains`, `regex`, `exact`, `json`, `script`) or by a `judge` model
  (`--judge models/foo.yaml`). Use `trials: N` + `acceptance.min_pass_rate`
  to measure consistency instead of one lucky sample.
- **agentic** — the model gets a `bash` tool in a throwaway workspace seeded
  from `files/`, and works the spec until it stops calling tools or exhausts
  `agent.max_steps`/`max_seconds`. Grading looks at the workspace, not the
  chat: `grade.py` runs the artifact and enforces the acceptance criteria.
  This is the "spec-driven loop" mode — no human feedback mid-run.
  Set `agent.command` to drive an external agent CLI (claude/codex/aider)
  instead of the built-in loop, terminal-bench style.

Notes on the built-in agent loop:
- Requires an endpoint with tool-calling (llama-server needs `--jinja`;
  vLLM needs `--enable-auto-tool-choice` + the right `--tool-call-parser`).
- Commands run as your user in the workspace — fine for tasks you wrote,
  not for untrusted ones. Use `agent.command` with a containerized agent
  if that ever changes.

## Versioning rules

- A test version is **immutable once it has runs**: edit ⇒ copy to `v<N+1>`.
  The manifest stores a content hash of the test dir, so a silently edited
  test is detectable (`test_sha` changes in `runs/index.jsonl`).
- Comparisons are only valid within the same `test@version` (and honestly,
  the same harness version — it's in the manifest too).

## Run artifacts

Every run dir is self-describing via `manifest.json`: harness version + git
sha, test name/version/content-hash, full model config and params, driver,
token usage, timestamps, and per-trial check results. `transcript.json` has
the full message/tool history; agentic runs keep the final workspace.

## Prior art this design borrows from

- **[Inspect AI](https://inspect.aisi.org.uk/)** (UK AI Safety Institute) —
  task = dataset + solver + scorer; rich per-run eval logs; agent loops and
  sandboxes. The heavyweight option if this harness ever feels too small;
  tests here (prompt/checks) would port to it cleanly.
- **[terminal-bench / Harbor](https://github.com/harbor-framework/terminal-bench)** —
  task = instruction + sandboxed workspace + test script + reference solution;
  versioned datasets. The agentic mode here is a mini version of this.
- **[promptfoo](https://www.promptfoo.dev/)** — YAML-first assertions and
  model matrices with CI ergonomics; the `checks:` block is the same idea.
- **[lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness)** —
  standardized academic benchmarks (MMLU etc.); use it for published numbers,
  this repo for "what I actually care about".
- **[Harness-Bench](https://arxiv.org/abs/2605.27922)** — evidence that
  results belong to a *model+harness pair*, not the model alone; hence the
  manifest records both.
