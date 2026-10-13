# iTailor - Copyright (c) 2026 Michael Bidollahkhani. All rights reserved. MIT License.
"""Model selection, the client's parsing, lessons retrieval, and the plan validator."""

import os
import tempfile
import unittest

from itailor import lessons, planner
from itailor.llm import client, discover, providers, select


class SelectTests(unittest.TestCase):
    def test_ahp_consistency(self):
        weights, cr = select.ahp_weights(select.DEFAULT_PAIRWISE)
        self.assertAlmostEqual(sum(weights), 1.0, places=6)
        self.assertLess(cr, 0.1)
        self.assertGreater(weights[0], weights[3])  # tools outranks speed
        _w, bad = select.ahp_weights([[1, 9, 1 / 9], [1 / 9, 1, 9], [9, 1 / 9, 1]])
        self.assertGreater(bad, 0.1)

    def test_params_from_name(self):
        self.assertEqual(select.params_from_name("qwen2.5-7b-instruct"), 7.0)
        self.assertEqual(select.params_from_name("Qwen3-30B-A3B"), 3.0)
        self.assertIsNone(select.params_from_name("gpt-5"))

    def test_hard_filters_and_roles(self):
        cands = [
            select.Candidate("big-coder-32b", "hosted", context=128000, native_tools=True),
            select.Candidate("tiny-3b", "local", local=True, context=8000, native_tools=False, params_b=3),
            select.Candidate("mid-7b-instruct", "local", local=True, context=32000, native_tools=None, params_b=7, est_tps=12, fits=True),
            select.Candidate("remote-reasoner", "hosted", context=64000, native_tools=True),
        ]
        chosen = select.select(cands, select.TaskProfile(context_needed=16000))
        self.assertIn(("tiny-3b", "no native tool calling and too small for the text protocol"), chosen.filtered_out)
        self.assertEqual(chosen.roles["coder"], "big-coder-32b")
        self.assertIn("planner", chosen.roles)
        self.assertTrue(chosen.fallbacks["coder"])
        local_only = select.select(cands, select.TaskProfile(context_needed=16000), select.Constraints(local_only=True))
        self.assertEqual(local_only.roles["coder"], "mid-7b-instruct")
        self.assertTrue(any("remote" in reason for _m, reason in local_only.filtered_out))

    def test_local_fit(self):
        fit = select.local_fit(4.7, ram_gb=16, vram_gb=0, ctx=16384, kv_kb_per_token=56)
        self.assertTrue(fit["fits"])
        self.assertGreater(fit["estTokensPerSecond"], 0)
        self.assertFalse(select.local_fit(18.6, ram_gb=8)["fits"])

    def test_bandit(self):
        path = os.path.join(tempfile.mkdtemp(), "bandit.json")
        bandit = select.BanditSelector(path, seed=1)
        for _ in range(20):
            bandit.update("good", True)
            bandit.update("bad", False)
        ranked = bandit.rerank([{"model": "bad", "score": 0.5}, {"model": "good", "score": 0.5}])
        self.assertEqual(ranked[0]["model"], "good")
        self.assertTrue(os.path.exists(path))


class ClientTests(unittest.TestCase):
    def test_endpoints(self):
        self.assertEqual(client.chat_endpoint("https://x/v1/"), "https://x/v1/chat/completions")
        self.assertEqual(client.models_endpoint("https://x/v1/chat/completions"), "https://x/v1/models")
        self.assertEqual(client.extract_context_window({"meta": {"context_length": 8192}}), 8192)

    def test_errors(self):
        self.assertTrue(client.is_retryable("HTTP 503 from the model API"))
        self.assertEqual(client.error_kind("HTTP 401 from the model API"), "auth")
        self.assertIn("rejected the API key", client.friendly_error("HTTP 401 x"))

    def test_parse_tool_call(self):
        call = client.parse_tool_call({"id": "c1", "function": {"name": "Read", "arguments": '{"path": "a"}'}})
        self.assertEqual((call.id, call.name, call.args), ("c1", "Read", {"path": "a"}))
        bad = client.parse_tool_call({"function": {"name": "Read", "arguments": "{oops"}})
        self.assertIn("__parse_error", bad.args)

    def test_fake_client(self):
        fake = client.FakeClient(["hello", client.tool_call_message("Read", {"path": "a"})])
        self.assertEqual(fake.chat([]).text, "hello")
        reply = fake.chat([])
        self.assertEqual(reply.tool_calls[0].name, "Read")
        self.assertEqual(fake.probe_tool_calling()["verdict"], "supported")

    def test_providers(self):
        self.assertEqual(providers.detect("https://api.openai.com/v1"), "openai")
        self.assertTrue(providers.known_tool_support("https://api.openai.com/v1", "gpt-4o"))
        self.assertIsNone(providers.known_tool_support("http://127.0.0.1:11434/v1", "llama3"))
        self.assertEqual(providers.filter_models("https://api.openai.com/v1", ["gpt-4o", "text-embedding-3"]), ["gpt-4o"])
        self.assertTrue(providers.is_local("http://localhost:1234/v1"))

    def test_discover_env_sources(self):
        os.environ["ITAILOR_BASE_URL"] = "https://example.test/v1"
        os.environ["ITAILOR_API_KEY"] = "k"
        try:
            sources = discover.from_environment()
            self.assertEqual(sources[0].base_url, "https://example.test/v1")
            self.assertEqual(sources[0].public()["api_key"], "[set]")
        finally:
            del os.environ["ITAILOR_BASE_URL"]
            del os.environ["ITAILOR_API_KEY"]


class LessonsTests(unittest.TestCase):
    def test_store_and_retrieve(self):
        tmp = tempfile.mkdtemp()
        store = lessons.LessonStore(os.path.join(tmp, "lessons.jsonl"))
        store.record_permission({"tool": "Install", "subject": "pip xgboost", "goal_tags": ["modeling"]}, "always", "analysis packages are fine")
        store.record_repair({"signature": "package|modulenotfounderror|fcntl", "os": "windows", "goal_tags": ["io"]}, "use msvcrt on Windows")
        store.record_consult({"question": "which column is the target?"}, "price")
        matches = store.retrieve({"tool": "Install", "subject": "pip lightgbm", "goal_tags": ["modeling"]}, kind="permission")
        self.assertTrue(matches)
        self.assertEqual(matches[0][0].decision, "always")
        repair = store.retrieve({"signature": "package|modulenotfounderror|fcntl", "os": "windows"}, kind="repair")
        self.assertIn("msvcrt", repair[0][0].fix)
        self.assertIn("LESSONS FROM EARLIER RUNS", lessons.prompt_block(matches))
        reloaded = lessons.LessonStore(os.path.join(tmp, "lessons.jsonl"))
        self.assertEqual(len(reloaded.all()), 3)
        proposals = reloaded.propose_rules()
        self.assertEqual(proposals[0]["rule"], "Install(pip:*)")
        self.assertTrue(reloaded.forget(proposals[0]["lessonId"]))
        self.assertEqual(len(reloaded.all()), 2)

    def test_confidence_laplace(self):
        lesson = lessons.Lesson(kind="repair", situation={}, uses=3, successes=3)
        self.assertAlmostEqual(lesson.confidence, 0.8)
        self.assertAlmostEqual(lessons.Lesson(kind="repair", situation={}).confidence, 0.5)


class PlannerTests(unittest.TestCase):
    def plan(self, steps):
        return planner.Plan(goal="g", steps=[planner.Step.from_dict(s) for s in steps])

    def test_valid_plan(self):
        tmp = tempfile.mkdtemp()
        open(os.path.join(tmp, "data.csv"), "w").write("a\n1\n")
        plan = self.plan([
            {"id": "s1", "title": "load", "kind": "code", "instruction": "x", "inputs": ["data.csv"], "outputs": ["clean.csv"], "check": {"type": "csv_rows", "path": "clean.csv"}},
            {"id": "s2", "title": "plot", "kind": "code", "instruction": "y", "inputs": ["clean.csv"], "outputs": ["p.png"], "depends_on": ["s1"], "check": {"type": "file_exists", "path": "p.png"}},
        ])
        self.assertEqual(planner.validate(plan, project=tmp, available_languages=["python"]), [])
        ordered, cyclic = planner.topological_order(plan.steps)
        self.assertEqual([s.id for s in ordered], ["s1", "s2"])
        self.assertIn("| s1 |", planner.to_markdown(plan))

    def test_violations(self):
        plan = self.plan([
            {"id": "a", "title": "a", "kind": "code", "instruction": "x", "depends_on": ["b"], "check": {"type": "exit_zero"}, "inputs": ["ghost.csv"]},
            {"id": "b", "title": "b", "kind": "weird", "instruction": "", "depends_on": ["a"], "check": {"type": "nope"}, "language": "cobol"},
        ])
        violations = planner.validate(plan, project=tempfile.mkdtemp(), available_languages=["python"])
        text = " ".join(violations)
        self.assertIn("cycle", text)
        self.assertIn("kind 'weird'", text)
        self.assertIn("check type 'nope'", text)
        self.assertIn("cobol", text)
        self.assertIn("instruction is empty", text)

    def test_missing_input_and_duplicate_ids(self):
        plan = self.plan([
            {"id": "s", "title": "a", "kind": "code", "instruction": "x", "inputs": ["ghost.csv"], "check": {"type": "exit_zero"}},
            {"id": "s", "title": "b", "kind": "code", "instruction": "y", "check": {"type": "file_exists"}},
        ])
        violations = planner.validate(plan, project=tempfile.mkdtemp())
        text = " ".join(violations)
        self.assertIn("used more than once", text)
        self.assertIn("ghost.csv", text)
        self.assertIn("needs a 'path'", text)

    def test_draft_with_fake_client(self):
        tmp = tempfile.mkdtemp()
        good = '{"restatement": "r", "assumptions": [], "steps": [{"id": "s1", "title": "t", "kind": "code", "instruction": "do", "outputs": ["o.txt"], "check": {"type": "file_exists", "path": "o.txt"}}]}'
        bad = '{"steps": [{"id": "s1", "title": "t", "kind": "code", "instruction": "do", "check": {"type": "nope"}}]}'
        fake = client.FakeClient([bad, good])
        plan, error = planner.draft(fake, "goal", project=tmp, available_languages=["python"])
        self.assertEqual(error, "")
        self.assertEqual(plan.steps[0].outputs, ["o.txt"])
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("REJECTED", fake.calls[1]["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
