# iTailor - Copyright (c) 2026 Michael Bidollahkhani. All rights reserved. MIT License.
"""The YAML subset and the project knowledge graph."""

import os
import tempfile
import unittest

from itailor import graph, yamllite


class YamlLiteTests(unittest.TestCase):
    def test_roundtrip(self):
        data = {
            "schemaVersion": "1.0", "n": 3, "f": 1.5, "t": True, "nothing": None, "empty_list": [], "empty_map": {},
            "text": "a: tricky # value", "multi": "line1\nline2", "plain": "hello world", "num_like": "0042",
            "list": ["a", 2, False, {"k": "v", "nested": {"x": [1, 2]}}, [1, 2]],
            "map": {"a": {"b": {"c": "deep"}}, "list_of_maps": [{"src": "s1", "rel": "produced", "dst": "f1"}, {"src": "s2", "rel": "r", "dst": "f2"}]},
            "quoted key with: colon": 1, "url": "https://x.y/z?a=1",
        }
        text = yamllite.dump(data)
        back = yamllite.load(text)
        self.assertEqual(back, data)

    def test_comments_ignored(self):
        self.assertEqual(yamllite.load("# comment\na: 1\n# another\nb:\n  - x\n"), {"a": 1, "b": ["x"]})

    def test_reserved_strings_quoted(self):
        self.assertEqual(yamllite.load(yamllite.dump({"s": "true", "e": "", "n": "12"})), {"s": "true", "e": "", "n": "12"})


class ProjectGraphTests(unittest.TestCase):
    def test_record_and_summary(self):
        project = tempfile.mkdtemp()
        path = graph.graph_path(project)
        g = graph.ProjectGraph(path)
        g.record_environment({"os": {"system": "Windows", "release": "11"}, "hardware": {"cores": 8, "ramTotalGb": 32, "gpus": []},
                              "interpreters": {"python": {"version": "3.12"}}, "packageManagers": {"pip": {}}, "apps": [1, 2, 3],
                              "pythonPackages": {"known": {"pandas": "2.2"}}})
        g.record_run_start("r1", "merge the csvs", "default")
        g.record_plan("r1", {"restatement": "merge", "revision": 0, "steps": [
            {"id": "s1", "title": "load", "kind": "code", "inputs": ["data.csv"], "outputs": ["merged.csv"], "check": {"type": "csv_rows", "path": "merged.csv"}},
            {"id": "s2", "title": "plot", "kind": "code", "depends_on": ["s1"], "inputs": ["merged.csv"], "outputs": ["p.png"], "check": {"type": "file_exists", "path": "p.png"}}]})
        g.record_step("r1", {"id": "s1", "title": "load", "status": "done", "attempts": 1, "summary": "ok", "check_results": [{"passed": True}]},
                      [{"path": "merged.csv", "size": 10}])
        g.record_decision("r1", {"tool": "Install", "command": "pip install xgboost", "decision": "always", "source": "user", "reason": "x"})
        g.record_run_end("r1", "partial", "half done", ["s2 failed"])
        g.save()
        self.assertTrue(os.path.exists(path))
        reloaded = graph.ProjectGraph(path)
        self.assertEqual(reloaded.nodes["run:r1"]["status"], "partial")
        self.assertEqual(reloaded.nodes["file:merged.csv"]["producedBy"], "step:r1:s1")
        self.assertIn("step:r1:s1", reloaded.neighbours("file:merged.csv", "produced", direction="in"))
        self.assertEqual(reloaded.neighbours("step:r1:s2", "depends_on"), ["step:r1:s1"])
        self.assertEqual(reloaded.data["environment"]["interpreters"]["python"], "3.12")
        text = reloaded.summary()
        self.assertIn("merge the csvs", text)
        self.assertIn("merged.csv (by s1)", text)
        self.assertIn("Install", text)
        self.assertIn("s2 failed", text)
        self.assertTrue(reloaded.find("xgboost"))

    def test_prune_keeps_recent_runs(self):
        g = graph.ProjectGraph(os.path.join(tempfile.mkdtemp(), ".itailor", "graph.yaml"), keep_runs=2)
        for i in range(4):
            g.record_run_start(f"r{i}", f"goal {i}")
            g.nodes[f"run:r{i}"]["started"] = f"2026-01-0{i + 1}T00:00:00Z"
            g.record_plan(f"r{i}", {"steps": [{"id": "s1", "title": "t", "check": {"type": "exit_zero"}}]})
        g.save()
        self.assertEqual(len(g.nodes_of("step")), 2)
        self.assertEqual(len(g.nodes_of("run")), 4)

    def test_empty_summary(self):
        g = graph.ProjectGraph(os.path.join(tempfile.mkdtemp(), "g.yaml"))
        self.assertEqual(g.summary(), "")


if __name__ == "__main__":
    unittest.main()
