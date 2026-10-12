"""Tests for the Intelligence Change Gate. Standard library only: python -m unittest discover tests"""
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

GATE_PATH = Path(__file__).resolve().parents[1] / "skills/agent-gates/assets/validate_intelligence_change.py"
spec = importlib.util.spec_from_file_location("gate", GATE_PATH)
gate = importlib.util.module_from_spec(spec)
sys.modules["gate"] = gate
spec.loader.exec_module(gate)

ACK = "I consciously accept merging this exact intelligence change without a passing experiment."
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
POLICY = {
    "schema_version": 1,
    "experiment_directory": "evals/experiments",
    "experiment_max_age_days": 45,
    "minimum_cases_by_stage": {"support_reply": 10},
    "override": {
        "directory": ".github/intelligence-overrides",
        "approvers": ["alice"],
        "max_age_days": 7,
        "acknowledgement": ACK,
    },
    "rules": [
        {"id": "support-reply", "paths": ["prompts/*.md", "src/context_builder.py"], "stages": ["support_reply"]},
        {"id": "settings", "paths": ["src/settings.py"], "line_patterns": ["SUPPORT_MODEL"], "stages": ["support_reply"]},
    ],
}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        self.write(".github/intelligence-policy.json", json.dumps(POLICY))
        self.write("prompts/reply.md", "You are a helpful support agent.\n")
        self.write("src/context_builder.py", "def build(history):\n    return history[-5:]\n")
        self.write("src/settings.py", "SUPPORT_MODEL = 'model-a'\nDB_TIMEOUT = 5\n")
        self.write("README.md", "docs\n")
        self.base = self.commit("base")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", msg)
        return git(self.repo, "rev-parse", "HEAD")

    def run_gate(self, head, pr=7):
        return gate.run_gate(self.repo, self.base, head, pr_number=pr, now=NOW)

    def manifest(self, fingerprint, candidate=0.96, passed=True, stages=("support_reply",)):
        sha = "a" * 64
        return {
            "schema_version": 1,
            "experiment_id": "context-builder-window-2026-10-11",
            "change_fingerprint": fingerprint,
            "affected_stages": list(stages),
            "run": {"completed_at": (NOW - timedelta(days=1)).isoformat(), "runner": "offline-replay", "runner_version": "abc123"},
            "dataset": {"id": "golden-support", "fingerprint": sha, "case_count": 50,
                        "breakdown": {"known_positive": 20, "known_negative": 30}},
            "baseline": {"name": "develop", "artifact_sha256": sha},
            "candidate": {"name": "this PR", "artifact_sha256": sha},
            "results": [{"stage": "support_reply", "kind": "quality", "metric": "verdict_agreement",
                         "direction": "higher_is_better", "baseline": 0.94, "candidate": candidate,
                         "minimum": 0.9, "maximum_regression": 0.0, "passed": passed}],
            "decision": "pass",
            "why_better": "Same verdicts on all known positives and one fewer false alarm on negatives.",
            "limitations": ["Only one failure mode in the sample."],
        }

    # --- tests ---
    def test_unprotected_change_needs_no_eval(self):
        self.write("README.md", "more docs\n")
        report = self.run_gate(self.commit("docs"))
        self.assertEqual(report.decision, "not_required")
        self.assertTrue(report.passed)

    def test_context_builder_change_without_evidence_is_blocked(self):
        self.write("src/context_builder.py", "def build(history):\n    return history[-2:]\n")
        report = self.run_gate(self.commit("shorter context"))
        self.assertEqual(report.decision, "blocked")
        self.assertFalse(report.passed)
        self.assertEqual(report.affected_stages, ["support_reply"])
        self.assertEqual(len(report.change_fingerprint), 64)
        self.assertIn("evals/experiments", gate._render(report))

    def test_matching_eval_report_passes(self):
        self.write("src/context_builder.py", "def build(history):\n    return history[-2:]\n")
        fp = self.run_gate(self.commit("shorter context")).change_fingerprint
        self.write("evals/experiments/ctx.json", json.dumps(self.manifest(fp)))
        report = self.run_gate(self.commit("evidence"))
        self.assertEqual(report.decision, "experiment", report.failures)
        self.assertTrue(report.passed)

    def test_later_protected_edit_makes_evidence_stale(self):
        self.write("src/context_builder.py", "def build(history):\n    return history[-2:]\n")
        fp = self.run_gate(self.commit("shorter context")).change_fingerprint
        self.write("evals/experiments/ctx.json", json.dumps(self.manifest(fp)))
        self.commit("evidence")
        self.write("src/context_builder.py", "def build(history):\n    return history[-3:]\n")
        report = self.run_gate(self.commit("tweak after eval"))
        self.assertEqual(report.decision, "blocked")
        self.assertNotEqual(report.change_fingerprint, fp)

    def test_passed_true_is_not_trusted(self):
        self.write("src/context_builder.py", "def build(history):\n    return history[-2:]\n")
        fp = self.run_gate(self.commit("shorter context")).change_fingerprint
        self.write("evals/experiments/ctx.json", json.dumps(self.manifest(fp, candidate=0.80, passed=True)))
        report = self.run_gate(self.commit("evidence that does not meet its bar"))
        self.assertEqual(report.decision, "blocked")
        self.assertTrue(any("does not satisfy its declared gate" in f for f in report.failures))

    def test_line_patterns_ignore_unrelated_settings(self):
        self.write("src/settings.py", "SUPPORT_MODEL = 'model-a'\nDB_TIMEOUT = 9\n")
        self.assertEqual(self.run_gate(self.commit("timeout")).decision, "not_required")

    def test_line_patterns_catch_model_switch(self):
        self.write("src/settings.py", "SUPPORT_MODEL = 'model-b'\nDB_TIMEOUT = 5\n")
        report = self.run_gate(self.commit("cheaper model"))
        self.assertEqual(report.decision, "blocked")
        self.assertEqual(report.matched_rules, ["settings"])

    def test_break_glass_override_is_recorded_not_passed_off(self):
        self.write("prompts/reply.md", "Flag every borderline case.\n")
        fp = self.run_gate(self.commit("favor recall")).change_fingerprint
        self.write(".github/intelligence-overrides/pr-7.json", json.dumps({
            "schema_version": 1, "pull_request": 7, "change_fingerprint": fp,
            "approved_by": "alice", "approved_at": (NOW - timedelta(hours=2)).isoformat(),
            "reason": "We now want recall over precision for this failure mode, so more false alarms are fine.",
            "risk": "Reviewers will see more false alarms; we will watch the false-positive rate and roll back.",
            "acknowledgement": ACK,
        }))
        report = self.run_gate(self.commit("override"))
        self.assertEqual(report.decision, "owner_override")
        self.assertTrue(report.passed)
        self.assertTrue(report.warnings)

    def test_override_for_another_pr_is_rejected(self):
        self.write("prompts/reply.md", "Flag every borderline case.\n")
        fp = self.run_gate(self.commit("favor recall")).change_fingerprint
        self.write(".github/intelligence-overrides/pr-7.json", json.dumps({
            "schema_version": 1, "pull_request": 8, "change_fingerprint": fp,
            "approved_by": "alice", "approved_at": (NOW - timedelta(hours=2)).isoformat(),
            "reason": "We now want recall over precision for this failure mode, so more false alarms are fine.",
            "risk": "Reviewers will see more false alarms; we will watch the false-positive rate and roll back.",
            "acknowledgement": ACK,
        }))
        self.assertEqual(self.run_gate(self.commit("wrong override")).decision, "blocked")

    def test_scaffold_prints_fingerprint_and_stages(self):
        self.write("prompts/reply.md", "New tone.\n")
        head = self.commit("tone")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = gate.main(["--repository", str(self.repo), "--base", self.base, "--head", head, "--scaffold-experiment"])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["affected_stages"], ["support_reply"])
        self.assertEqual(data["change_fingerprint"], self.run_gate(head).change_fingerprint)


if __name__ == "__main__":
    unittest.main()
