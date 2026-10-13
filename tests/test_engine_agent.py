# iTailor - Copyright (c) 2026 Michael Bidollahkhani. All rights reserved. MIT License.
"""End to end with a scripted model: the script engine repairs real failures; the agent plans, executes, verifies, reports."""

import json
import os
import tempfile
import unittest

from itailor import Agent, checks
from itailor.engines.script import ScriptEngine
from itailor.llm.client import FakeClient, tool_call_message
from itailor.oversight import CallbackOversight, AutoDeny
from itailor.runs import RunDir
from itailor.sandbox import RootsPolicy
from itailor.transcript import Transcript


def allow_all():
    return CallbackOversight(permission=lambda r: "allow", consult=lambda r: (r.options or ["ok"])[0])


class ScriptEngineTests(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp()
        os.environ["ITAILOR_HOME"] = tempfile.mkdtemp()

    def tearDown(self):
        os.environ.pop("ITAILOR_HOME", None)

    def engine(self, replies):
        policy = RootsPolicy(self.project, project_root=self.project, mode="writes")
        return ScriptEngine(FakeClient(replies), cwd=self.project, runs_dir=os.path.join(self.project, ".itailor", "runs"),
                            policy=policy, settings={"maxIterations": 4, "runTimeoutSeconds": 60})

    def test_clean_run_with_checks(self):
        engine = self.engine(["```python\nprint('hello world')\nopen('out.txt', 'w').write('x')\n```"])
        result = engine.run("say hello", language="python", checks=[checks.file_exists("out.txt"), checks.stdout_matches("hello")])
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.iterations, 1)
        self.assertTrue(any(f.endswith("out.txt") for f in result.files))
        self.assertTrue(all(c["passed"] for c in result.checks))

    def test_repair_from_real_error(self):
        engine = self.engine(["```python\nprint(undefined_name)\n```", "```python\nprint('fixed')\n```"])
        result = engine.run("print something", language="python")
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.iterations, 2)
        self.assertEqual(result.attempts[0]["failure"]["kind"], "script")
        self.assertEqual(result.attempts[0]["failure"]["etype"], "NameError")

    def test_failed_check_is_evidence(self):
        engine = self.engine(["```python\nprint('no file yet')\n```", "```python\nopen('report.md', 'w').write('# r')\nprint('done')\n```"])
        result = engine.run("write a report", language="python", checks=[checks.file_exists("report.md")])
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.iterations, 2)

    def test_fixed_point_stops(self):
        same = "```python\nraise ValueError('always the same')\n```"
        engine = self.engine([same, same, same, same])
        result = engine.run("fail", language="python")
        self.assertFalse(result.success)
        self.assertIn("fixed point", result.stop_reason)
        self.assertEqual(result.iterations, 2)

    def test_sandbox_blocks_write_outside(self):
        outside = os.path.join(tempfile.mkdtemp(), "x.txt").replace("\\", "/")
        engine = self.engine([f"```python\nopen('{outside}', 'w').write('1')\n```", "```python\nopen('in.txt','w').write('1')\n```"])
        result = engine.run("write", language="python")
        self.assertTrue(result.success)
        self.assertEqual(result.attempts[0]["failure"]["kind"], "sandbox")
        self.assertFalse(os.path.exists(outside))


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp()
        os.environ["ITAILOR_HOME"] = tempfile.mkdtemp()

    def tearDown(self):
        os.environ.pop("ITAILOR_HOME", None)

    def test_goal_loop_end_to_end(self):
        understand = json.dumps({"restatement": "write result.txt", "unknowns": [], "assumptions": ["plain text is fine"], "tags": ["files"]})
        plan = json.dumps({"restatement": "write result.txt", "assumptions": [], "steps": [
            {"id": "s1", "title": "Write the file", "kind": "code", "instruction": "Create result.txt containing 42",
             "outputs": ["result.txt"], "check": {"type": "file_exists", "path": "result.txt"}, "tags": ["files"]}]})
        script = [understand, plan,
                  tool_call_message("RunCode", {"language": "python", "code": "open('result.txt','w').write('42')\nprint('wrote 42')"}),
                  "I wrote result.txt with 42 and verified it.", "Findings: result.txt holds 42."]
        fake = FakeClient(script)
        seen = []
        agent = Agent(self.project, client=fake, oversight=allow_all(), on_event=lambda e: seen.append(e.type), discover_env=False)
        result = agent.run("write a file result.txt containing 42", checks=[checks.file_exists("result.txt")])
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertTrue(result.ok)
        self.assertTrue(os.path.exists(os.path.join(self.project, "result.txt")))
        self.assertEqual(result.steps[0]["status"], "done")
        self.assertTrue(result.checks[0]["passed"])
        self.assertTrue(os.path.exists(result.report_path))
        report = open(result.report_path, encoding="utf-8").read()
        self.assertIn("Findings", report)
        self.assertIn("result.txt", report)
        self.assertIn("permission_request", seen)
        self.assertIn("step_done", seen)
        run_dir = RunDir.open(self.project, result.run_id)
        rows = Transcript.read(run_dir.transcript_path)
        self.assertTrue(any(r.get("type") == "tool_call" for r in rows))
        self.assertTrue(os.path.exists(run_dir.plan_md))
        self.assertTrue(os.path.exists(os.path.join(run_dir.artifacts_dir, "result.txt")))

    def test_denied_permission_is_reported(self):
        plan = json.dumps({"steps": [{"id": "s1", "title": "t", "kind": "code", "instruction": "write out.txt",
                                      "outputs": ["out.txt"], "check": {"type": "file_exists", "path": "out.txt"}}]})
        script = [json.dumps({"restatement": "r", "unknowns": [], "assumptions": []}), plan,
                  tool_call_message("RunCode", {"language": "python", "code": "open('out.txt','w').write('1')"}),
                  "I could not run the script because permission was denied.",
                  # repair attempts (3) each: tool call denied, then text
                  tool_call_message("RunCode", {"language": "python", "code": "open('out.txt','w').write('1')"}), "denied again",
                  tool_call_message("RunCode", {"language": "python", "code": "open('out.txt','w').write('1')"}), "denied again",
                  tool_call_message("RunCode", {"language": "python", "code": "open('out.txt','w').write('1')"}), "denied again",
                  "Summary: nothing could run."]
        agent = Agent(self.project, client=FakeClient(script), oversight=AutoDeny(), discover_env=False)
        result = agent.run("write out.txt")
        self.assertIn(result.status, ("partial", "succeeded"))
        self.assertFalse(result.ok)
        self.assertEqual(result.steps[0]["status"], "failed")
        self.assertTrue(any("failed" in item for item in (agent.current_run.read_json("state.json") or {}).get("openItems", [])))

    def test_text_protocol_mode(self):
        plan = json.dumps({"steps": [{"id": "s1", "title": "t", "kind": "code", "instruction": "write a.txt",
                                      "outputs": ["a.txt"], "check": {"type": "file_exists", "path": "a.txt"}}]})
        script = [json.dumps({"restatement": "r", "unknowns": [], "assumptions": []}), plan,
                  'Writing the file.\n<tool name="Write">\n{"path": "a.txt", "content": "hello"}\n</tool>',
                  "Done, a.txt written.", "Findings: a.txt written."]
        agent = Agent(self.project, client=FakeClient(script, native_tools=False), oversight=allow_all(), discover_env=False,
                      permissions="acceptEdits")
        result = agent.run("write a.txt")
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(open(os.path.join(self.project, "a.txt")).read(), "hello")

    def test_consultation_from_understanding(self):
        understand = json.dumps({"restatement": "r", "unknowns": [{"question": "CSV or JSON?", "options": ["csv", "json"], "materiality": "high"}],
                                 "assumptions": [], "tags": []})
        plan = json.dumps({"steps": [{"id": "s1", "title": "t", "kind": "code", "instruction": "write it",
                                      "outputs": ["o.csv"], "check": {"type": "file_exists", "path": "o.csv"}}]})
        script = [understand, plan, tool_call_message("Write", {"path": "o.csv", "content": "a\n1\n"}), "done", "findings"]
        answers = []
        oversight = CallbackOversight(permission=lambda r: "allow", consult=lambda r: answers.append(r.question) or "csv")
        agent = Agent(self.project, client=FakeClient(script), oversight=oversight, discover_env=False)
        result = agent.run("export the data")
        self.assertEqual(answers, ["CSV or JSON?"])
        state = agent.current_run.read_json("state.json")
        self.assertIn("CSV or JSON? -> csv", state["clarifications"])
        self.assertEqual(result.status, "succeeded")
        self.assertTrue(any(l.kind == "consult" for l in agent.lessons.all()))

    def test_plan_mode_is_read_only(self):
        script = [tool_call_message("Write", {"path": "x.txt", "content": "no"}), "Plan: do X then Y."]
        agent = Agent(self.project, client=FakeClient(script), oversight=allow_all(), discover_env=False, permissions="plan")
        turn = agent.run_turn("make a plan")
        self.assertTrue(turn.ok)
        self.assertFalse(os.path.exists(os.path.join(self.project, "x.txt")))
        self.assertIn("Unknown tool", json.dumps(turn.tool_results[0]["output"]))


if __name__ == "__main__":
    unittest.main()
