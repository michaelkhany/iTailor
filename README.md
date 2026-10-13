# iTailor

**The patch mechanism as a Python library.** A natural-language request becomes code or a command that is written, **run against the real environment**, read back from its actual output, verified by checks it did not write, and repaired until it works. Deterministic methods decide, gate, verify and stop; the model only produces candidates.

Michael Bidollahkhani's design, first shipped in [DAYA Studio](../daya-pro-main)'s iTailor engine and re-implemented for a code editor as [Patch Code](../patch-code). This library is the mechanism's standalone home; both can consume it. Copyright (c) 2026 Michael Bidollahkhani, MIT License.

The full design, with the scientific grounding of every mechanism, is in [ITAILOR_LIBRARY_DESIGN.md](../ITAILOR_LIBRARY_DESIGN.md).

## Install

```bash
pip install -e .          # from this folder; zero runtime dependencies, Python >= 3.10
itailor init              # discover the machine, find a model, write config, (optionally) smoke test
itailor "describe the CSVs in data/ and plot anything interesting"
```

A model is any OpenAI-compatible endpoint. `itailor init` looks for one without asking first: `ITAILOR_BASE_URL` / `ITAILOR_API_KEY` / `ITAILOR_MODEL` (or `OPENAI_*`), config files, and local servers already running (Ollama, LM Studio, llama.cpp, vLLM). With several candidates it picks by a documented multi-criteria ranking (`itailor model` shows the reasons).

## Python API

```python
import itailor
from itailor import checks

agent = itailor.Agent("~/work/study-7", oversight=itailor.ConsoleOversight())
result = agent.run(
    "Merge every CSV in data/ into one table, plot monthly totals, write a short report.",
    checks=[checks.file_exists("monthly_totals.png"), checks.file_exists("report.md")],
)
print(result.status, result.summary)
print(result.files, result.report_path)       # .itailor/runs/<id>/report.md

# stream events instead
for event in itailor.Agent(".").stream("..."):
    print(event.type, event.message)

# the one-shot script engine on its own
r = itailor.run_script("print the five largest files here", project=".")
print(r.stdout, r.files)
```

What a run does (design §2.2): understand the goal (ask the human only for a *material* unknown, by the mixed-initiative rule) → discover what this machine has → draft a plan the validator accepts (every step has an acceptance check) → execute each step with tools → verify the check → repair within budget, replan within budget → write `report.md` / `report.json`, the plan, a redacted transcript and the produced files under `.itailor/runs/<id>/`.

## Command line

```
itailor "task"                 run a goal here, interactive prompts
itailor --print "task"         headless: nothing asks, denied by default, JSON result on stdout
itailor --jsonl "task"         headless: events as JSON lines (drive it from another program)
itailor script "task" --language r
itailor env [--refresh]        the capability manifest: OS, hardware, interpreters, packages, installed apps
itailor model [--probe]        model sources, candidates, the selection and its reasons
itailor lessons list|forget ID|propose
itailor runs | report RUN_ID | resume RUN_ID
itailor init [--yes] [--smoke] | doctor
```

## What the agent can do

| Class | Tools |
| --- | --- |
| read | `Read` `Glob` `Grep` `ListDir` `KnowledgeGraph` `WebSearch` `WebFetch` `EnvInfo` `FindProgram` `FindApp` |
| edit | `Edit` `Write` |
| run | `RunCode` (any language with an interpreter on PATH; a missing package is installed with permission and the script re-run) |
| shell | `Shell` (one argv command, no shell; read-only inspections run without asking) |
| install | `Install` (pip, uv, npm, R/CRAN, gem, go, cargo, dotnet, composer) |
| system | `OpenWith` (drive an installed application, headless where documented) |
| human | `AskUser` (only for the five consultation triggers) |

## Permissions and policy

Claude Code's model, extended into a chain of deterministic deciders (`itailor.policy`): hard blocks → organisation policy → deny rules → risk score → plan pre-approval → ask rules → allow rules → lessons (hints only) → mode default → the human. Deny from any layer wins. Rules in any settings layer:

```json
{ "permissions": { "allow": ["Shell(git:*)", "Edit(src/**)"], "deny": ["Write(.env*)", "Shell(git push*)"], "ask": ["Edit(package.json)"] } }
```

Modes: `default`, `acceptEdits`, `plan`, `bypassPermissions`. Settings layers: `~/.itailor/config.json` → `.itailor/settings.json` → `.itailor/settings.local.json` → `ITAILOR_*` environment. `ITAILOR.md` is read like `CLAUDE.md`. Hooks (`PreToolUse`/`PostToolUse`) run commands; exit code 2 blocks.

A timeout is a denial. A refusal is remembered for the run. `always` grants last the session. Every decision is in the transcript with the full chain that produced it.

## Lessons

Every human decision and every repair that finally worked is a JSON line in `.itailor/lessons.jsonl` (or `~/.itailor/lessons.jsonl`), retrieved by BM25 + tag overlap and shown to the model and in the permission prompt. Lessons inform; they never authorise. `itailor lessons propose` lists the `always` answers as candidate rules for you to accept.

## Project knowledge graph

`.itailor/graph.yaml` is what iTailor knows about the project across runs: the environment it last saw, each run with its plan and steps, the files produced and by which step, the permission answers you gave, the lessons, and the open items. A compact view goes into every prompt (so a later run knows what an earlier one left behind) and the `ProjectState` tool lets the model query it. It is derived data: `itailor graph --rebuild` regenerates it from the run folders, and it is never the oracle for anything. It is written as YAML by a small dependency-free emitter so you can read and edit it.

## Safety posture

Generated code runs on this machine, as you. The environment a child sees is an allow-list, never the parent's secrets. Generated Python runs under an audit-hook guard (`itailor/guard.py`) that refuses writes outside the run, starting another process, and (in `strict` mode) reads outside the project and network access; `ITAILOR_SANDBOX=strict|writes|off`. This is a real boundary against generated code doing something destructive, not a jail against a hostile human; OS-level confinement belongs to an execution-context adapter.

## Status

Implemented: the pure core (events, sandbox, guard, languages, model client, text protocol), environment and installed-app discovery, model source discovery and AHP-weighted selection, the policy engine and gate, oversight adapters, lessons, settings, hooks, the tool set, the one-shot script engine with classification and stopping rule, the goal loop with planner validation, checks, reports, resume, the CLI, and the evaluation harness skeleton. Not yet: the persistent-kernel and program-mode engines (Phase 2), recipes, container and SSH execution contexts.

## Development

```bash
python -m unittest discover -s tests -v
python evals/run_evals.py --list
```

Tests use a scripted `FakeClient`; no network and no model are needed. The guard tests spawn real subprocesses.
