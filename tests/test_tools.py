# iTailor - Copyright (c) 2026 Michael Bidollahkhani. All rights reserved. MIT License.
"""Tools: shell classification and execution, install mapping, file tools, the guard, env discovery, knowledge graph."""

import os
import subprocess
import sys
import tempfile
import unittest

from itailor import env, guard, hooks, sandbox
from itailor.tools import fs, install, knowledge, shell, default_registry
from itailor.tools.registry import ToolContext


class ShellTests(unittest.TestCase):
    def test_classify(self):
        self.assertTrue(shell.classify("git status")["readOnly"])
        self.assertTrue(shell.classify("pip list")["readOnly"])
        self.assertFalse(shell.classify("pip install x")["readOnly"])
        self.assertFalse(shell.classify("ls | grep x")["readOnly"])
        self.assertFalse(shell.classify("python -c 'print(1)'")["readOnly"])
        self.assertFalse(shell.classify("git config user.name me")["readOnly"])
        self.assertTrue(shell.classify("git config --get user.name")["readOnly"])
        self.assertTrue(shell.classify("rm -rf /")["destructive"])

    def test_split(self):
        self.assertEqual(shell.split_command('echo "a b" c'), ["echo", "a b", "c"])

    def test_run_argv(self):
        result = shell.run_argv([sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"], cwd=tempfile.mkdtemp(), timeout=30)
        self.assertEqual(result["exitCode"], 3)
        self.assertIn("hi", result["stdout"])
        self.assertFalse(result["success"])

    def test_run_timeout(self):
        result = shell.run_argv([sys.executable, "-c", "import time; time.sleep(5)"], cwd=tempfile.mkdtemp(), timeout=1)
        self.assertIn("timed out", result["error"])

    def test_missing_program(self):
        result = shell.run_argv(["definitely-not-a-program-xyz"], cwd=tempfile.mkdtemp())
        self.assertIn("not found", result["error"].lower())

    def test_stdin(self):
        result = shell.run_argv([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], cwd=tempfile.mkdtemp(), stdin="abc")
        self.assertIn("ABC", result["stdout"])


class InstallTests(unittest.TestCase):
    def test_missing_module(self):
        self.assertEqual(install.missing_module_from("No module named 'sklearn'")["module"], "sklearn")
        self.assertEqual(install.missing_module_from("Cannot find module 'lodash'")["manager"], "npm")
        self.assertIsNone(install.missing_module_from("Cannot find module 'fs'"))
        self.assertIsNone(install.missing_module_from("No module named 'os'"))
        self.assertEqual(install.missing_module_from("there is no package called 'ggplot2'")["manager"], "r")

    def test_package_for(self):
        self.assertEqual(install.package_for("pip", "sklearn"), "scikit-learn")
        self.assertEqual(install.package_for("pip", "matplotlib.pyplot"), "matplotlib")
        self.assertEqual(install.package_for("pip", "foo.bar"), "foo")

    def test_installable_names(self):
        self.assertTrue(install.is_installable_name("pandas>=2.0"))
        self.assertTrue(install.is_installable_name("@scope/pkg"))
        self.assertFalse(install.is_installable_name("-r requirements.txt"))
        self.assertFalse(install.is_installable_name("pip"))
        self.assertFalse(install.is_installable_name("x; rm -rf /"))

    def test_install_command(self):
        argv = install.install_command("pip", ["numpy"], python="py")
        self.assertEqual(argv[:4], ["py", "-m", "pip", "install"])
        self.assertEqual(install.install_command("npm", ["lodash"])[:2], ["npm", "install"])
        self.assertIsNone(install.install_command("nope", ["x"]))
        self.assertIsNone(install.install_command("pip", ["pip"]))


class FsTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "src"))
        with open(os.path.join(self.root, "src", "a.py"), "w", encoding="utf-8") as fh:
            fh.write("import os\n\ndef f(x, y):\n    return x\n\nclass C:\n    pass\n\nvalue = 1\n")

    def test_read_write_edit(self):
        read = fs.read_file(self.root, {"path": "src/a.py"})
        self.assertEqual(read["totalLines"], 10)
        self.assertIn("1\timport os", read["content"])
        wrote = fs.write_file(self.root, {"path": "new/b.txt", "content": "x\ny"})
        self.assertEqual(wrote["action"], "created")
        edited = fs.edit_file(self.root, {"path": "src/a.py", "old_string": "value = 1", "new_string": "value = 2"})
        self.assertEqual(edited["replaced"], 1)
        self.assertIn("value = 2", open(os.path.join(self.root, "src", "a.py")).read())
        self.assertIn("error", fs.edit_file(self.root, {"path": "src/a.py", "old_string": "nope", "new_string": "x"}))
        self.assertIn("error", fs.read_file(self.root, {"path": "../etc"}))

    def test_glob_grep_list(self):
        self.assertEqual(fs.glob(self.root, {"pattern": "**/*.py"})["count"], 1)
        self.assertEqual(fs.grep(self.root, {"pattern": r"def \w+"})["matches"], 1)
        self.assertIn("src/", fs.list_dir(self.root, {})["listing"])

    def test_knowledge_graph(self):
        graph = knowledge.analyse(open(os.path.join(self.root, "src", "a.py")).read(), "python")
        self.assertEqual(graph["functions"], ["f(x, y)"])
        self.assertEqual(graph["classes"], ["C"])
        self.assertIn("value", graph["variables"])
        js = knowledge.analyse("const x = 1;\nfunction go(a) {}\n", "javascript")
        self.assertEqual(js["functions"], ["go(a)"])


class GuardTests(unittest.TestCase):
    def test_write_outside_refused(self):
        allowed = tempfile.mkdtemp()
        outside = tempfile.mkdtemp()
        script = os.path.join(allowed, "s.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(f"open(r'{os.path.join(allowed, 'ok.txt')}', 'w').write('1')\n"
                     f"open(r'{os.path.join(outside, 'bad.txt')}', 'w').write('2')\n")
        env_vars = sandbox.child_env({"ITAILOR_SANDBOX": "writes", "ITAILOR_SANDBOX_WRITE": allowed})
        result = subprocess.run([sys.executable, sandbox.GUARD_PATH, script], capture_output=True, text=True, env=env_vars, cwd=allowed, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("iTailor sandbox", result.stderr)
        self.assertTrue(os.path.exists(os.path.join(allowed, "ok.txt")))
        self.assertFalse(os.path.exists(os.path.join(outside, "bad.txt")))

    def test_spawn_refused(self):
        allowed = tempfile.mkdtemp()
        script = os.path.join(allowed, "s.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write("import subprocess\nsubprocess.run(['echo', 'x'])\n")
        env_vars = sandbox.child_env({"ITAILOR_SANDBOX": "writes", "ITAILOR_SANDBOX_WRITE": allowed})
        result = subprocess.run([sys.executable, sandbox.GUARD_PATH, script], capture_output=True, text=True, env=env_vars, cwd=allowed, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("may not start another process", result.stderr)

    def test_off_mode_runs_plain(self):
        allowed = tempfile.mkdtemp()
        script = os.path.join(allowed, "s.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write("print('fine')\n")
        env_vars = sandbox.child_env({"ITAILOR_SANDBOX": "off"})
        result = subprocess.run([sys.executable, sandbox.GUARD_PATH, script], capture_output=True, text=True, env=env_vars, cwd=allowed, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertIn("fine", result.stdout)


class EnvTests(unittest.TestCase):
    def test_build_manifest_quick(self):
        manifest = env.build(include_apps=False, include_network=False)
        self.assertIn("python", manifest["interpreters"])
        self.assertGreater(manifest["hardware"]["cores"], 0)
        text = env.summary(manifest)
        self.assertIn("Interpreters", text)

    def test_find_program(self):
        found = env.find_program("python")
        self.assertTrue(found["found"])
        missing = env.find_program("definitely-not-installed-xyz")
        self.assertFalse(missing["found"])
        self.assertTrue(missing["installHint"])

    def test_find_app_with_manifest(self):
        manifest = {"apps": [{"name": "Microsoft Excel", "version": "16", "exe": "", "path": ""}]}
        result = env.find_app("excel", manifest)
        self.assertTrue(result["found"])
        self.assertIn("openpyxl", result["matches"][0]["headless"])


class HooksTests(unittest.TestCase):
    def test_pre_hook_blocks(self):
        tmp = tempfile.mkdtemp()
        hook = os.path.join(tmp, "hook.py")
        with open(hook, "w", encoding="utf-8") as fh:
            fh.write("import sys, json\npayload = json.load(sys.stdin)\nprint('nope', file=sys.stderr)\nsys.exit(2 if payload['tool'] == 'Shell' else 0)\n")
        settings = {"hooks": {"PreToolUse": [{"matcher": "Shell|Write", "hooks": [{"type": "command", "command": f'"{sys.executable}" "{hook}"'}]}]}}
        blocked = hooks.run("PreToolUse", settings=settings, cwd=tmp, tool_name="Shell", payload={"args": {}})
        self.assertTrue(blocked["block"])
        self.assertIn("nope", blocked["reason"])
        passed = hooks.run("PreToolUse", settings=settings, cwd=tmp, tool_name="Write", payload={"args": {}})
        self.assertFalse(passed["block"])
        self.assertEqual(len(passed["results"]), 1)


class RegistryTests(unittest.TestCase):
    def test_specs_and_availability(self):
        registry = default_registry()
        ctx = ToolContext(cwd=tempfile.mkdtemp(), settings={"webSearch": False})
        names = registry.available_names(ctx)
        self.assertNotIn("WebSearch", names)
        self.assertIn("RunCode", names)
        spec = registry.specs(["Read"])[0]
        self.assertEqual(spec["function"]["name"], "Read")
        self.assertEqual(registry.describe("Shell", {"command": "ls"}), "Shell: ls")
        self.assertIn("pip install", registry.command_of("Install", {"manager": "pip", "packages": ["x"]}))


if __name__ == "__main__":
    unittest.main()
