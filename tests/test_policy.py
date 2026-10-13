# iTailor - Copyright (c) 2026 Michael Bidollahkhani. All rights reserved. MIT License.
"""The policy engine: rules, risk, interrupt rule, chain, and the gate with oversight."""

import unittest

from itailor import policy
from itailor.oversight import CallbackOversight, AutoDeny
from itailor.permissions import Gate
from itailor.policy import Action, Context, PolicyEngine, default_chain


class RulesTests(unittest.TestCase):
    def test_parse_and_match(self):
        self.assertTrue(policy.rule_matches("Shell(npm test *)", "Shell", {"command": "npm test src"}))
        self.assertTrue(policy.rule_matches("Shell(git:*)", "Shell", {"command": "git status"}))
        self.assertFalse(policy.rule_matches("Shell(git:*)", "Shell", {"command": "npm i"}))
        self.assertTrue(policy.rule_matches("Edit(src/**)", "Edit", {"path": "src/a/b.py"}))
        self.assertTrue(policy.rule_matches("Write(.env*)", "Write", {"path": ".env.local"}))
        self.assertTrue(policy.rule_matches("WebSearch", "WebSearch", {"query": "x"}))
        self.assertTrue(policy.rule_matches("*", "Anything", {}))
        self.assertFalse(policy.rule_matches("Read", "Write", {}))
        self.assertIsNone(policy.parse_rule("Not a rule!"))

    def test_destructive(self):
        self.assertTrue(policy.is_destructive("rm -rf /"))
        self.assertTrue(policy.is_destructive("git push origin main --force"))
        self.assertTrue(policy.is_destructive("format C:"))
        self.assertFalse(policy.is_destructive("rm build/tmp.txt"))
        self.assertFalse(policy.is_destructive("git status"))


class RiskTests(unittest.TestCase):
    def test_levels(self):
        read = policy.assess(Action(tool="Read", args={"path": "a.py"}))
        self.assertEqual(read.level, "very_low")
        delete = policy.assess(Action(tool="Shell", args={"command": "rm -r data"}, command="rm -r data"))
        self.assertGreaterEqual(delete.score, 12)
        secret = policy.assess(Action(tool="Write", args={"path": ".env"}, paths=["/p/.env"]), project_root="/p")
        self.assertEqual(secret.factors["dataSensitivity"], 5)
        egress = policy.assess(Action(tool="Shell", args={"command": "scp x host:"}, command="scp x host:"))
        self.assertEqual(egress.factors["egress"], 4)

    def test_blast_radius_outside_project(self):
        a = policy.assess(Action(tool="Write", args={"path": "x"}, paths=["/elsewhere/x"]), project_root="/proj")
        self.assertGreaterEqual(a.factors["blastRadius"], 4)


class InterruptTests(unittest.TestCase):
    def test_horvitz_rule(self):
        self.assertFalse(policy.should_interrupt(0.1, 1.0, 1.0))
        self.assertTrue(policy.should_interrupt(0.6, 12.0, 1.0))

    def test_value_of_information(self):
        # two states equally likely; action A good in state 0, action B good in state 1
        evpi = policy.value_of_information([0.5, 0.5], [[10, 0], [0, 10]])
        self.assertAlmostEqual(evpi, 5.0)
        self.assertEqual(policy.value_of_information([0.5, 0.5], [[10, 10]]), 0.0)

    def test_consult_verdict(self):
        self.assertFalse(policy.consult_verdict("nonsense")["ask"])
        self.assertFalse(policy.consult_verdict("ambiguous_goal", "low")["ask"])
        self.assertTrue(policy.consult_verdict("ambiguous_goal", "high")["ask"])
        self.assertFalse(policy.consult_verdict("ambiguous_goal", "high", options=["only"])["ask"])
        self.assertTrue(policy.consult_verdict("irreversible_or_outward")["ask"])


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = PolicyEngine(default_chain(project_root="/proj"))

    def decide(self, action, **ctx):
        decision, trace = self.engine.evaluate(action, Context(**ctx))
        return decision, trace

    def test_read_allowed_by_mode(self):
        d, t = self.decide(Action(tool="Read", args={"path": "a"}))
        self.assertEqual(d.verdict, policy.ALLOW)
        self.assertEqual(d.source, "mode")

    def test_deny_overrides_allow(self):
        d, _ = self.decide(Action(tool="Shell", args={"command": "git push"}, command="git push"),
                           rules={"allow": ["Shell"], "deny": ["Shell(git push*)"], "ask": []})
        self.assertEqual(d.verdict, policy.DENY)

    def test_hard_block_wins(self):
        d, _ = self.decide(Action(tool="Shell", args={"command": "rm -rf /"}, command="rm -rf /"), mode="bypassPermissions")
        self.assertEqual(d.verdict, policy.DENY)
        self.assertEqual(d.source, "hard-block")

    def test_ask_rule_beats_mode(self):
        d, _ = self.decide(Action(tool="Edit", args={"path": "package.json"}), mode="acceptEdits",
                           rules={"allow": [], "deny": [], "ask": ["Edit(package.json)"]})
        self.assertEqual(d.verdict, policy.ASK)

    def test_risk_forces_ask_despite_allow_rule(self):
        d, _ = self.decide(Action(tool="Shell", args={"command": "rm -r data"}, command="rm -r data"),
                           rules={"allow": ["Shell"], "deny": [], "ask": []})
        self.assertEqual(d.verdict, policy.ASK)
        self.assertEqual(d.source, "risk")

    def test_plan_mode_denies_edit(self):
        d, _ = self.decide(Action(tool="Write", args={"path": "x"}), mode="plan")
        self.assertEqual(d.verdict, policy.DENY)

    def test_preapproved_step(self):
        action = Action(tool="RunCode", args={"language": "python", "code": "print(1)"})
        d, _ = self.decide(action, plan_approved=True, approved_signatures={action.signature})
        self.assertEqual(d.verdict, policy.ALLOW)

    def test_lesson_hints_attached(self):
        d, t = self.decide(Action(tool="Install", args={"manager": "pip", "packages": ["x"]}),
                           lessons=[{"decision": "always", "rationale": "analysis packages are fine"}])
        self.assertEqual(d.verdict, policy.ASK)
        self.assertTrue(any("always" in h for h in d.hints))
        self.assertTrue(any(s["decider"] == "lessons" for s in t.steps))

    def test_org_policy(self):
        engine = PolicyEngine(default_chain(org_policy={"forbidNetwork": True}))
        d, _ = engine.evaluate(Action(tool="WebSearch", args={"query": "x"}), Context())
        self.assertEqual(d.verdict, policy.DENY)


class GateTests(unittest.TestCase):
    def test_allow_once_and_refusal_memory(self):
        answers = iter(["allow", "deny"])
        decisions = []
        gate = Gate(oversight=CallbackOversight(permission=lambda r: next(answers)), emit=lambda t, m="", **d: decisions.append((t, d)))
        a = Action(tool="RunCode", args={"language": "python", "code": "print(1)"}, summary="run")
        self.assertTrue(gate(a).allowed)
        b = Action(tool="Shell", args={"command": "pip install x"}, command="pip install x", summary="install")
        self.assertFalse(gate(b).allowed)
        self.assertFalse(gate(b).allowed)  # remembered, not asked again
        self.assertEqual(len([d for t, d in decisions if t == "permission_request"]), 2)

    def test_always_grant(self):
        gate = Gate(oversight=CallbackOversight(permission=lambda r: "always"))
        a = Action(tool="RunCode", args={"language": "python", "code": "1"}, summary="run")
        self.assertTrue(gate(a).allowed)
        b = Action(tool="RunCode", args={"language": "python", "code": "2"}, summary="run2")
        self.assertTrue(gate(b).allowed)
        self.assertIn("run", gate.grants)

    def test_headless_denies(self):
        gate = Gate(oversight=AutoDeny())
        self.assertFalse(gate(Action(tool="Shell", args={"command": "npm install"}, command="npm install")).allowed)
        self.assertTrue(gate(Action(tool="Shell", args={"command": "git status"}, command="git status", read_only=True)).allowed)

    def test_no_oversight(self):
        gate = Gate(oversight=None)
        self.assertFalse(gate(Action(tool="Write", args={"path": "a"})).allowed)


if __name__ == "__main__":
    unittest.main()
