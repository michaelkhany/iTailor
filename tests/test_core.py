# iTailor - Copyright (c) 2026 Michael Bidollahkhani. All rights reserved. MIT License.
"""Core modules: text protocol, JSON schema, repair classification, checks, settings, cache, transcript, runs, languages, sandbox."""

import json
import os
import tempfile
import unittest

from itailor import cache, checks, events, jsonschema_lite, languages, repair, runs, sandbox, settings, textprotocol, transcript


class TextProtocolTests(unittest.TestCase):
    def test_tool_block(self):
        parsed = textprotocol.parse('I will read it.\n<tool name="Read">\n{"path": "a.py"}\n</tool>')
        self.assertEqual(parsed["content"], "I will read it.")
        self.assertEqual(parsed["tool_calls"][0]["name"], "Read")
        self.assertEqual(parsed["tool_calls"][0]["args"], {"path": "a.py"})

    def test_fenced_and_json_line(self):
        parsed = textprotocol.parse('```tool\n{"tool": "Glob", "args": {"pattern": "*.py"}}\n```')
        self.assertEqual(parsed["tool_calls"][0]["name"], "Glob")
        parsed = textprotocol.parse('{"tool": "ListDir", "path": "."}')
        self.assertEqual(parsed["tool_calls"][0]["args"], {"path": "."})

    def test_plain_text(self):
        parsed = textprotocol.parse("All done.")
        self.assertEqual(parsed["tool_calls"], [])
        self.assertEqual(parsed["content"], "All done.")

    def test_bad_json_is_reported(self):
        parsed = textprotocol.parse('<tool name="Read">{not json</tool>')
        self.assertIn("__parse_error", parsed["tool_calls"][0]["args"])

    def test_instructions_mention_tools(self):
        text = textprotocol.instructions([{"name": "Read", "description": "read", "parameters": {"properties": {"path": {"type": "string"}}, "required": ["path"]}}])
        self.assertIn("- Read: read", text)
        self.assertIn("path: string", text)


class JsonSchemaTests(unittest.TestCase):
    def test_validate(self):
        schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer", "minimum": 1}, "b": {"type": "array", "items": {"type": "string"}}}}
        self.assertEqual(jsonschema_lite.validate({"a": 2, "b": ["x"]}, schema), [])
        errors = jsonschema_lite.validate({"a": 0, "b": [1]}, schema)
        self.assertTrue(any("minimum" in e for e in errors))
        self.assertTrue(any("$.b[0]" in e for e in errors))
        self.assertTrue(jsonschema_lite.validate({}, schema))

    def test_extract_json(self):
        self.assertEqual(jsonschema_lite.extract_json('here: ```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(jsonschema_lite.extract_json('x {"a": [1,2]} y'), {"a": [1, 2]})
        with self.assertRaises(ValueError):
            jsonschema_lite.extract_json("nothing")


class RepairTests(unittest.TestCase):
    def test_package_failure(self):
        f = repair.classify_failure("", "Traceback...\nModuleNotFoundError: No module named 'sklearn'", 1)
        self.assertEqual(f.kind, "package")
        self.assertEqual(f.module, "sklearn")
        self.assertEqual(f.manager, "pip")

    def test_stdlib_is_not_a_package(self):
        f = repair.classify_failure("", "ModuleNotFoundError: No module named 'json'", 1)
        self.assertNotEqual(f.kind, "package")

    def test_sandbox_unrepairable(self):
        f = repair.classify_failure("", "PermissionError: iTailor sandbox: this script may not read '/etc/x'. This was requested by an installed library, not by this script", 1)
        self.assertEqual(f.kind, "sandbox")
        self.assertFalse(f.repairable)

    def test_timeout_and_syntax(self):
        self.assertEqual(repair.classify_failure("", "", None, error="The command timed out after 10s.").kind, "timeout")
        self.assertEqual(repair.classify_failure("", "  File x\nSyntaxError: invalid syntax", 1).kind, "syntax")
        self.assertEqual(repair.classify_failure("ok", "", 0).kind, "none")

    def test_signature_normalises_numbers_and_paths(self):
        a = repair.error_signature("KeyError", "'col' at line 12 in C:\\a\\b.py")
        b = repair.error_signature("KeyError", "'col' at line 99 in C:\\x\\y.py")
        self.assertEqual(a, b)

    def test_stopping_rule_fixed_point(self):
        rule = repair.StoppingRule(max_attempts=6, repeat_limit=2)
        rule.record("sig-a")
        self.assertFalse(rule.should_stop()[0])
        rule.record("sig-a")
        stop, why = rule.should_stop()
        self.assertTrue(stop)
        self.assertIn("fixed point", why)

    def test_stopping_rule_budget(self):
        rule = repair.StoppingRule(max_attempts=2)
        rule.record("a")
        rule.record("b")
        self.assertTrue(rule.should_stop()[0])

    def test_extract_code_and_fragment(self):
        self.assertEqual(repair.extract_code("```python\nprint(1)\n```", "python"), "print(1)")
        self.assertEqual(repair.extract_code("print(2)"), "print(2)")
        old = "\n".join(f"line{i}" for i in range(20))
        self.assertTrue(repair.is_fragment("line1\nline2", old))
        self.assertFalse(repair.is_fragment(old, old))


class ChecksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, "data.csv"), "w", encoding="utf-8") as fh:
            fh.write("a,b\n1,2\n3,4\n")
        with open(os.path.join(self.tmp, "out.json"), "w", encoding="utf-8") as fh:
            json.dump({"k": 1}, fh)

    def test_file_and_csv_and_json(self):
        ctx = checks.CheckContext(project=self.tmp, stdout="R^2 = 0.91", exit_code=0)
        results = checks.run_checks([checks.file_exists("data.csv"), checks.csv_rows("data.csv", 2, ["a", "b"]),
                                     checks.json_valid("out.json", ["k"]), checks.stdout_matches(r"R\^2 = 0\.\d+"),
                                     checks.exit_zero(), checks.file_exists("missing.txt")], ctx)
        self.assertEqual([r.passed for r in results], [True, True, True, True, True, False])

    def test_from_spec(self):
        self.assertEqual(checks.from_spec({"type": "csv_rows", "path": "x.csv", "min_rows": 3}).kind, "csv_rows")
        self.assertIsNone(checks.from_spec({"type": "nope"}))

    def test_fresh_file_check_rejects_leftovers(self):
        import time
        stale = checks.from_spec({"type": "file_exists", "path": "data.csv", "fresh": True})
        future = checks.CheckContext(project=self.tmp, since=time.time() + 100)
        self.assertFalse(stale.run(future).passed)
        self.assertIn("stale", stale.run(future).detail)
        past = checks.CheckContext(project=self.tmp, since=time.time() - 100)
        self.assertTrue(stale.run(past).passed)
        self.assertTrue(checks.file_exists("data.csv").run(future).passed)  # not fresh: leftovers are fine

    def test_custom_never_raises(self):
        def boom(ctx):
            raise RuntimeError("x")
        result = checks.custom(boom, "boom").run(checks.CheckContext(project=self.tmp))
        self.assertFalse(result.passed)
        self.assertIn("RuntimeError", result.detail)


class SettingsTests(unittest.TestCase):
    def test_layers_merge(self):
        tmp = tempfile.mkdtemp()
        home = tempfile.mkdtemp()
        os.environ["ITAILOR_HOME"] = home
        try:
            settings.write_layer(settings.user_config_path(), {"model": "m1", "permissions": {"allow": ["WebSearch"]}})
            settings.write_layer(settings.project_settings_path(tmp), {"permissions": {"deny": ["Shell(git push*)"]}, "budgets": {"steps": 5}})
            settings.write_layer(settings.local_settings_path(tmp), {"model": "m2", "permissions": {"allow": ["Shell(git:*)"]}})
            loaded = settings.load(tmp, {"permissionMode": "acceptEdits"})
            self.assertEqual(loaded["model"], "m2")
            self.assertEqual(loaded["permissions"]["allow"], ["WebSearch", "Shell(git:*)"])
            self.assertEqual(loaded["permissions"]["deny"], ["Shell(git push*)"])
            self.assertEqual(loaded["budgets"]["steps"], 5)
            self.assertEqual(loaded["budgets"]["minutes"], 30)
            self.assertEqual(loaded["permissionMode"], "acceptEdits")
            self.assertEqual([s["name"] for s in loaded["_sources"]][:3], ["user", "project", "local"])
        finally:
            del os.environ["ITAILOR_HOME"]

    def test_comments_tolerated(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "s.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{ // comment\n "model": "x", }')
        self.assertEqual(settings.read_json(path)["model"], "x")

    def test_context_files(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "ITAILOR.md"), "w", encoding="utf-8") as fh:
            fh.write("# rules\n- be nice")
        found = settings.load_context_files(tmp)
        self.assertTrue(any(f["scope"] == "project" for f in found))


class SmallModulesTests(unittest.TestCase):
    def test_cache(self):
        c = cache.ContentCache(tempfile.mkdtemp())
        value, hit = c.memo(("a", 1), lambda: {"v": 1})
        self.assertFalse(hit)
        value2, hit2 = c.memo(("a", 1), lambda: {"v": 2})
        self.assertTrue(hit2)
        self.assertEqual(value2, {"v": 1})

    def test_transcript_redacts(self):
        path = os.path.join(tempfile.mkdtemp(), "t.jsonl")
        t = transcript.Transcript(path, {"api_key": "secret"})
        t.record(events.Event(type="x", message="m", data={"password": "p", "ok": 1}))
        rows = transcript.Transcript.read(path)
        self.assertEqual(rows[0]["metadata"]["api_key"], "[redacted]")
        self.assertEqual(rows[1]["password"], "[redacted]")
        self.assertEqual(rows[1]["ok"], 1)

    def test_runs(self):
        project = tempfile.mkdtemp()
        rd = runs.RunDir.create(project, "merge the csvs")
        rd.write_json("state.json", {"goal": "merge", "status": "succeeded"})
        self.assertTrue(os.path.isdir(rd.artifacts_dir))
        listed = runs.list_runs(project)
        self.assertEqual(listed[0]["status"], "succeeded")
        src = os.path.join(project, "x.txt")
        open(src, "w").write("hi")
        art = rd.collect_artifact(src, "s1")
        self.assertEqual(art["name"], "x.txt")
        self.assertTrue(os.path.exists(os.path.join(rd.artifacts_dir, "x.txt")))

    def test_events_stream(self):
        seen = []
        stream = events.EventStream(lambda e: seen.append(e.type))
        stream.emit("think", "x", a=1)
        stream.close()
        collected = [e.type for e in stream]
        self.assertEqual(seen, ["think"])
        self.assertEqual(collected, ["think"])

    def test_cancel_token(self):
        token = events.CancelToken()
        token.check()
        token.cancel()
        with self.assertRaises(events.RunCancelled):
            token.check()

    def test_redact(self):
        self.assertEqual(events.redact({"Authorization": "x", "n": [1, {"token": "t"}]}), {"Authorization": "[redacted]", "n": [1, {"token": "[redacted]"}]})


class LanguagesTests(unittest.TestCase):
    def test_lookup(self):
        self.assertEqual(languages.by_id("py").id, "python")
        self.assertEqual(languages.by_filename("x.R").id, "r")
        self.assertEqual(languages.by_filename("Dockerfile").id, "dockerfile")
        self.assertEqual(languages.resolve(".ts").id, "typescript")
        self.assertIn("python", languages.available())
        self.assertEqual(languages.comment_line(languages.by_id("html"), "m"), "<!-- m -->")

    def test_choose(self):
        self.assertEqual(languages.choose("auto", "plot with ggplot2"), "r" if languages.program_available(languages.BY_ID["r"]) else "python")
        self.assertEqual(languages.choose("python", "anything"), "python")


class SandboxTests(unittest.TestCase):
    def test_child_env_allowlist(self):
        base = {"PATH": "/bin", "ITAILOR_API_KEY": "secret", "OPENAI_API_KEY": "s", "HTTPS_PROXY": "p"}
        env = sandbox.child_env({"X": "1"}, base=base)
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["HTTPS_PROXY"], "p")
        self.assertNotIn("ITAILOR_API_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["X"], "1")
        self.assertIn("OPENAI_API_KEY", sandbox.redacted_names(base))

    def test_resolve_path_confines(self):
        root = tempfile.mkdtemp()
        inside = sandbox.resolve_path(root, "a/b.txt")
        self.assertTrue(inside.startswith(os.path.abspath(root)))
        with self.assertRaises(sandbox.PathOutsideProject):
            sandbox.resolve_path(root, "../outside.txt")
        self.assertTrue(sandbox.resolve_path(root, "../outside.txt", confine=False))

    def test_roots_policy(self):
        root = tempfile.mkdtemp()
        policy = sandbox.RootsPolicy(root, project_root=root, mode="strict")
        env = policy.child_paths()
        self.assertEqual(env["ITAILOR_SANDBOX"], "strict")
        self.assertIn(os.path.abspath(root), env["ITAILOR_SANDBOX_WRITE"])
        self.assertIn("No network", policy.prompt_rules())
        self.assertEqual(policy.guard_argv("python", "s.py")[1], sandbox.GUARD_PATH)
        off = sandbox.RootsPolicy(root, mode="off")
        self.assertEqual(off.guard_argv("python", "s.py"), ["python", "s.py"])


if __name__ == "__main__":
    unittest.main()
