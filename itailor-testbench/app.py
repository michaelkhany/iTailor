# iTailor testbench - Copyright (c) 2026 Michael Bidollahkhani. All rights reserved. MIT License.
# The patch mechanism is Michael Bidollahkhani's original design; Patchbook's iTailor was its first application (b-0240331).
"""
A Flask test bench for the iTailor library: a browser UI to run goals and
scripts against a sample workspace, answer permission and consultation
requests, watch the event stream, and inspect the environment manifest, the
model selection, the lessons, the runs and their reports, and the project
knowledge graph.

It imports the COPY of the library in this folder (./itailor), never an
installed one, so what you test is what you synced:

    python sync_itailor.py        # copies ../itailor/itailor -> ./itailor
    pip install -r requirements.txt
    python app.py                 # http://127.0.0.1:5055
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
# The local copy wins over anything installed.
sys.path.insert(0, HERE)

import itailor  # noqa: E402
from itailor import checks as checks_module, env as env_module, settings as settings_module  # noqa: E402
from itailor.graph import ProjectGraph, graph_path  # noqa: E402
from itailor.llm import discover as llm_discover, select as llm_select, providers  # noqa: E402
from itailor.oversight import QueueOversight  # noqa: E402
from itailor.runs import RunDir, list_runs  # noqa: E402
from itailor.transcript import Transcript  # noqa: E402

from flask import Flask, Response, jsonify, request, send_from_directory, render_template  # noqa: E402

IMPORTED_FROM = os.path.dirname(os.path.abspath(itailor.__file__))
WORKSPACE = os.path.abspath(os.environ.get("ITAILOR_TESTBENCH_WORKSPACE") or os.path.join(HERE, "workspace"))
os.makedirs(WORKSPACE, exist_ok=True)

app = Flask(__name__, template_folder=os.path.join(HERE, "templates"), static_folder=os.path.join(HERE, "static"))
RUNS: dict[str, dict] = {}
RUNS_LOCK = threading.Lock()
MAX_LOG = 5000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _settings() -> dict:
    return settings_module.load(WORKSPACE)


def _public_settings(s: dict) -> dict:
    keys = ("baseUrl", "model", "apiKeyEnv", "permissionMode", "profile", "toolCalling", "streaming", "webSearch",
            "permissions", "sandbox", "budgets", "privacy", "approvePlans", "modelBandit", "discoverApps")
    out = {k: s.get(k) for k in keys}
    out["apiKeySet"] = bool(os.environ.get(s.get("apiKeyEnv") or "ITAILOR_API_KEY") or os.environ.get("ITAILOR_API_KEY"))
    out["_sources"] = s.get("_sources")
    out["_errors"] = s.get("_errors")
    return out


def _parse_checks(specs):
    out = []
    for spec in specs or []:
        spec = str(spec).strip()
        if not spec:
            continue
        kind, _, arg = spec.partition(":")
        if kind in ("file", "file_exists"):
            out.append(checks_module.file_exists(arg))
        elif kind == "stdout":
            out.append(checks_module.stdout_matches(arg))
        elif kind == "csv":
            path, _, rows = arg.partition("@")
            out.append(checks_module.csv_rows(path, int(rows or 1)))
        elif kind == "json":
            out.append(checks_module.json_valid(arg))
        elif kind == "cmd":
            out.append(checks_module.command_exit_zero(arg))
    return out


def _fake_client(goal: str):
    """A scripted model for exercising the bench without an API key: it writes
    fake_output.txt through RunCode, then reports. Decides what to answer from
    the prompt it is given, so it survives any call order."""
    import json as _json
    from itailor.llm.client import FakeClient, tool_call_message

    def script(messages, tools):
        system = str(messages[0].get("content") or "") if messages else ""
        if "understanding half" in system:
            return _json.dumps({"restatement": f"(fake) {goal[:80]}", "unknowns": [], "assumptions": ["this is the scripted fake model"], "tags": ["fake"]})
        if "planning half" in system:
            return _json.dumps({"restatement": f"(fake) {goal[:80]}", "assumptions": [], "steps": [
                {"id": "s1", "title": "Write fake_output.txt", "kind": "code", "instruction": "Write the goal text into fake_output.txt and print a line.",
                 "outputs": ["fake_output.txt"], "check": {"type": "file_exists", "path": "fake_output.txt"}, "tags": ["fake"]}]})
        if "findings section" in system:
            return "Findings (fake model): fake_output.txt was written and verified by the file_exists check."
        if "iTailor, an autonomous coding agent" in system:
            return "```python\nopen('fake_output.txt', 'w', encoding='utf-8').write('fake script output')\nprint('fake script ran')\n```"
        had_tool_result = any(m.get("role") == "tool" or str(m.get("content") or "").startswith("<tool_result") for m in messages)
        if tools and not had_tool_result:
            code = f"open('fake_output.txt', 'w', encoding='utf-8').write({goal!r})\nprint('wrote fake_output.txt')"
            return tool_call_message("RunCode", {"language": "python", "code": code, "reason": "fake model demo"})
        return "Done (fake model): fake_output.txt written; see the step check for verification."
    return FakeClient(script)


def _start_run(goal: str, *, engine: str, mode: str | None, checks: list, language: str, model: str | None) -> dict:
    run_id = uuid.uuid4().hex[:10]
    events: queue.Queue = queue.Queue()
    log: list = []
    entry = {"id": run_id, "goal": goal, "engine": engine, "status": "starting", "started": time.time(),
             "events": events, "log": log, "result": None, "error": None, "agent": None, "oversight": None}

    def publish(payload: dict) -> None:
        log.append(payload)
        if len(log) > MAX_LOG:
            del log[: len(log) - MAX_LOG]
        events.put(payload)

    def on_request(payload: dict) -> None:
        publish({"type": "oversight", "message": payload.get("summary") or payload.get("question") or "", **payload})

    oversight = QueueOversight(on_request=on_request)
    overrides = {}
    if mode:
        overrides["permissionMode"] = mode
    fake = model == "fake"
    agent = itailor.Agent(WORKSPACE, settings=overrides, oversight=oversight, model=None if fake else (model or None),
                          client=_fake_client(goal) if fake else None, on_event=lambda e: publish(e.to_dict()))
    entry["agent"] = agent
    entry["oversight"] = oversight

    def worker() -> None:
        entry["status"] = "running"
        try:
            if engine == "script":
                result = agent.run_script(goal, language=language or "auto", checks=checks)
                entry["result"] = result.to_dict()
                entry["status"] = "succeeded" if result.success else "failed"
            elif engine == "chat":
                turn = agent.run_turn(goal)
                entry["result"] = {"ok": turn.ok, "text": turn.text, "steps": turn.steps, "error": turn.error,
                                   "files": turn.files, "toolResults": [{"name": r["name"], "ok": r["ok"]} for r in turn.tool_results]}
                entry["status"] = "succeeded" if turn.ok else "failed"
            else:
                result = agent.run(goal, checks=checks)
                entry["result"] = result.to_dict()
                entry["status"] = result.status
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["status"] = "error"
            publish({"type": "error", "message": entry["error"]})
        finally:
            entry["finished"] = time.time()
            publish({"type": "bench_end", "message": entry["status"], "status": entry["status"], "result": entry["result"]})
            events.put(None)

    with RUNS_LOCK:
        RUNS[run_id] = entry
    threading.Thread(target=worker, daemon=True).start()
    return entry


def _public_run(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k not in ("events", "log", "agent", "oversight")}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    s = _settings()
    return jsonify({"version": itailor.__version__, "importedFrom": IMPORTED_FROM, "workspace": WORKSPACE,
                    "python": sys.version.split()[0], "settings": _public_settings(s),
                    "activeRuns": [{"id": e["id"], "status": e["status"], "goal": e["goal"][:80]} for e in RUNS.values()
                                   if e["status"] in ("starting", "running")]})


@app.get("/api/env")
def api_env():
    refresh = request.args.get("refresh") == "1"
    manifest = env_module.discover(WORKSPACE, refresh=refresh)
    return jsonify({"summary": env_module.summary(manifest, max_apps=40), "manifest": manifest})


@app.get("/api/find")
def api_find():
    name = request.args.get("name", "")
    manifest = env_module.discover(WORKSPACE)
    return jsonify({"program": env_module.find_program(name), "app": env_module.find_app(name, manifest)})


@app.get("/api/models")
def api_models():
    probe = request.args.get("probe") == "1"
    sources = llm_discover.discover(WORKSPACE)
    s = _settings()
    if s.get("baseUrl"):
        key_env = s.get("apiKeyEnv") or "ITAILOR_API_KEY"
        sources.insert(0, llm_discover.Source(id="settings", kind="config", base_url=s["baseUrl"], api_key=os.environ.get(key_env, ""),
                                              key_env=key_env, model="" if s.get("model") in ("auto", None) else s.get("model"),
                                              provider=providers.detect(s["baseUrl"]), local=providers.is_local(s["baseUrl"]), note="from settings"))
    usable = [x for x in sources if x.base_url and (x.api_key or x.local)]
    llm_discover.fill_models(usable, timeout=15)
    caps_path = os.path.join(settings_module.user_dir(), "model_tool_capabilities.json")
    caps = {}
    try:
        caps = json.load(open(caps_path, "r", encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    if probe:
        from itailor.llm.client import OpenAICompatibleClient
        for source in usable:
            client = OpenAICompatibleClient(source.config())
            for model in source.models[:30]:
                key = f"{source.base_url}|{model}"
                if key in caps or providers.known_tool_support(source.base_url, model) is True:
                    continue
                verdict = client.probe_tool_calling(model=model, timeout=25)
                if verdict["verdict"] != "inconclusive":
                    caps[key] = {"nativeToolCalling": verdict["verdict"] == "supported", "detail": verdict["detail"][:200]}
        os.makedirs(os.path.dirname(caps_path), exist_ok=True)
        json.dump(caps, open(caps_path, "w", encoding="utf-8"), indent=1)
    manifest = env_module.discover(WORKSPACE)
    candidates = llm_select.candidates_from_sources(usable, caps, env_module.machine_for_selection(manifest))
    privacy = s.get("privacy") or {}
    selection = llm_select.select(candidates, llm_select.TaskProfile(),
                                  llm_select.Constraints(local_only=bool(privacy.get("localOnly")), exclude=s.get("autoModelExclude") or []))
    return jsonify({"sources": [x.public() for x in sources], "selection": selection.to_dict(),
                    "capabilities": {k: v for k, v in caps.items() if any(k.startswith(x.base_url) for x in usable)}})


@app.get("/api/settings")
def api_settings_get():
    return jsonify(_public_settings(_settings()))


@app.post("/api/settings")
def api_settings_post():
    body = request.get_json(force=True) or {}
    patch = {k: body[k] for k in ("baseUrl", "model", "apiKeyEnv", "permissionMode", "profile", "toolCalling",
                                  "webSearch", "approvePlans", "discoverApps") if k in body}
    if "sandboxMode" in body:
        patch["sandbox"] = {"mode": body["sandboxMode"]}
    if "localOnly" in body:
        patch["privacy"] = {"localOnly": bool(body["localOnly"])}
    if body.get("apiKey"):
        # In memory only: the key is never written to a file by the test bench.
        var = patch.get("apiKeyEnv") or body.get("apiKeyEnv") or "ITAILOR_API_KEY"
        os.environ[var] = str(body["apiKey"])
        patch["apiKeyEnv"] = var
    if patch:
        settings_module.write_layer(settings_module.local_settings_path(WORKSPACE), patch)
    return jsonify(_public_settings(_settings()))


@app.post("/api/run")
def api_run():
    body = request.get_json(force=True) or {}
    goal = str(body.get("goal") or "").strip()
    if not goal:
        return jsonify({"error": "goal is required"}), 400
    entry = _start_run(goal, engine=str(body.get("engine") or "goal"), mode=body.get("mode") or None,
                       checks=_parse_checks(body.get("checks") or []), language=str(body.get("language") or "auto"),
                       model=body.get("model"))
    return jsonify(_public_run(entry))


@app.get("/api/run/<run_id>")
def api_run_get(run_id):
    entry = RUNS.get(run_id)
    if not entry:
        return jsonify({"error": "no such run"}), 404
    data = _public_run(entry)
    data["pending"] = entry["oversight"].pending() if entry["oversight"] else []
    data["log"] = entry["log"][-400:]
    return jsonify(data)


@app.get("/api/run/<run_id>/events")
def api_run_events(run_id):
    entry = RUNS.get(run_id)
    if not entry:
        return jsonify({"error": "no such run"}), 404

    def generate():
        yield "retry: 3000\n\n"
        for item in entry["log"]:
            yield "data: " + json.dumps(item, ensure_ascii=False, default=str) + "\n\n"
        if entry["status"] not in ("starting", "running"):
            return
        while True:
            try:
                item = entry["events"].get(timeout=15)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            if item is None:
                return
            yield "data: " + json.dumps(item, ensure_ascii=False, default=str) + "\n\n"
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/run/<run_id>/answer")
def api_run_answer(run_id):
    entry = RUNS.get(run_id)
    if not entry or not entry["oversight"]:
        return jsonify({"error": "no such run"}), 404
    body = request.get_json(force=True) or {}
    answer = body.get("decision") if body.get("decision") is not None else body.get("answer")
    ok = entry["oversight"].resolve(str(body.get("requestId") or ""), answer)
    return jsonify({"ok": ok})


@app.post("/api/run/<run_id>/stop")
def api_run_stop(run_id):
    entry = RUNS.get(run_id)
    if not entry:
        return jsonify({"error": "no such run"}), 404
    entry["agent"].cancel()
    return jsonify({"ok": True})


@app.get("/api/runs")
def api_runs():
    return jsonify({"runs": list_runs(WORKSPACE), "live": [_public_run(e) for e in RUNS.values()]})


@app.get("/api/runs/<run_id>/report")
def api_runs_report(run_id):
    try:
        run_dir = RunDir.open(WORKSPACE, run_id)
    except FileNotFoundError:
        return jsonify({"error": "no such run"}), 404
    report = open(run_dir.report_md, "r", encoding="utf-8").read() if os.path.isfile(run_dir.report_md) else ""
    plan = open(run_dir.plan_md, "r", encoding="utf-8").read() if os.path.isfile(run_dir.plan_md) else ""
    return jsonify({"report": report, "plan": plan, "state": run_dir.read_json("state.json"),
                    "transcript": Transcript.read(run_dir.transcript_path)[-300:]})


@app.get("/api/lessons")
def api_lessons():
    agent = itailor.Agent(WORKSPACE, discover_env=False)
    return jsonify({"lessons": [l.to_dict() | {"confidence": l.confidence} for l in agent.lessons.all()],
                    "proposals": agent.lessons.propose_rules()})


@app.delete("/api/lessons/<lesson_id>")
def api_lessons_delete(lesson_id):
    agent = itailor.Agent(WORKSPACE, discover_env=False)
    return jsonify({"ok": agent.lessons.forget(lesson_id)})


@app.get("/api/graph")
def api_graph():
    path = graph_path(WORKSPACE)
    if request.args.get("rebuild") == "1":
        g = ProjectGraph(path)
        g.rebuild(WORKSPACE)
        g.save()
    g = ProjectGraph(path)
    text = open(path, "r", encoding="utf-8").read() if os.path.isfile(path) else ""
    return jsonify({"summary": g.summary(max_runs=10, max_files=40, max_decisions=20), "yaml": text,
                    "nodes": len(g.nodes), "edges": len(g.edges), "path": path})


@app.get("/api/files")
def api_files():
    items = []
    for root, dirs, files in os.walk(WORKSPACE):
        dirs[:] = [d for d in dirs if d not in (".itailor", "__pycache__", ".git")]
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, WORKSPACE).replace(os.sep, "/")
            try:
                items.append({"path": rel, "size": os.path.getsize(full), "mtime": os.path.getmtime(full)})
            except OSError:
                continue
    items.sort(key=lambda i: -i["mtime"])
    return jsonify({"files": items[:500]})


@app.get("/files/<path:rel>")
def serve_file(rel):
    return send_from_directory(WORKSPACE, rel)


@app.get("/api/file")
def api_file():
    rel = request.args.get("path", "")
    full = os.path.abspath(os.path.join(WORKSPACE, rel))
    if not full.startswith(WORKSPACE) or not os.path.isfile(full):
        return jsonify({"error": "no such file"}), 404
    with open(full, "rb") as handle:
        data = handle.read(200000)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return jsonify({"path": rel, "binary": True, "size": os.path.getsize(full)})
    return jsonify({"path": rel, "text": text, "truncated": os.path.getsize(full) > 200000})


if __name__ == "__main__":
    port = int(os.environ.get("PORT") or 5055)
    print(f"iTailor testbench: library {itailor.__version__} from {IMPORTED_FROM}")
    print(f"workspace: {WORKSPACE}")
    print(f"open http://127.0.0.1:{port}")
    app.run(host=os.environ.get("ITAILOR_TESTBENCH_HOST", "127.0.0.1"), port=port, debug=False, threaded=True)
