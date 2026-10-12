"""Intelligence Change Gate: fail closed when LLM behavior changes lack exact evidence.

This is a version of the eval gate that Zoroval (https://zoroval.com) runs in its own CI,
with Zoroval-specific paths and messages removed. Background:
https://zoroval.com/blog/llm-eval-gates-agentic-coding

The gate deliberately uses only the Python standard library. CI can therefore inspect a
pull request before installing or executing product dependencies. Eval reports (experiment
manifests) and break-glass owner overrides are bound to a fingerprint of the protected diff,
so a later prompt, context builder, model or parser edit cannot inherit an earlier approval
accidentally.

Usage:
    python validate_intelligence_change.py --base origin/main --head HEAD [--pr-number N]
    python validate_intelligence_change.py --base origin/main --head HEAD --scaffold-experiment
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable, Mapping


POLICY_SCHEMA_VERSION = 1
EVIDENCE_SCHEMA_VERSION = 1
DEFAULT_POLICY_PATH = Path(".github/intelligence-policy.json")
SHA256_PATTERN = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")


class IntelligenceGateError(ValueError):
    """The policy, repository diff, or evidence is malformed."""


@dataclass
class GateReport:
    affected_stages: list[str]
    protected_paths: list[str]
    matched_rules: list[str]
    change_fingerprint: str
    decision: str
    evidence_path: str = ""
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    experiment_directory: str = ""
    override_path_hint: str = ""

    @property
    def passed(self) -> bool:
        return not self.failures and self.decision in {
            "not_required", "experiment", "owner_override",
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "passed": self.passed,
            "decision": self.decision,
            "affected_stages": self.affected_stages,
            "protected_paths": self.protected_paths,
            "matched_rules": self.matched_rules,
            "change_fingerprint": self.change_fingerprint,
            "evidence_path": self.evidence_path,
            "failures": self.failures,
            "warnings": self.warnings,
        }


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise IntelligenceGateError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout


def _load_json(value: str, source: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise IntelligenceGateError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise IntelligenceGateError(f"{source} must contain a JSON object")
    return payload


def load_policy(repository: Path, policy_path: Path = DEFAULT_POLICY_PATH) -> Mapping[str, Any]:
    path = policy_path if policy_path.is_absolute() else repository / policy_path
    payload = _load_json(path.read_text(encoding="utf-8"), str(path))
    if payload.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise IntelligenceGateError(
            f"Unsupported intelligence policy schema: {payload.get('schema_version')}"
        )
    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        raise IntelligenceGateError("Intelligence policy requires at least one rule")
    return payload


def _changed_paths(repository: Path, base: str, head: str) -> list[str]:
    output = _git(
        repository, "diff", "--name-only", "--no-renames", "--no-ext-diff",
        f"{base}...{head}", "--",
    )
    return sorted({line.strip() for line in output.splitlines() if line.strip()})


def _changed_lines(repository: Path, base: str, head: str, path: str) -> list[str]:
    output = _git(
        repository, "diff", "--unified=0", "--no-renames", "--no-ext-diff",
        f"{base}...{head}", "--", path,
    )
    return [
        line[1:]
        for line in output.splitlines()
        if (line.startswith("+") or line.startswith("-"))
        and not line.startswith("+++")
        and not line.startswith("---")
    ]


def _matching_rules(
    policy: Mapping[str, Any], repository: Path, base: str, head: str,
    changed_paths: Iterable[str],
) -> tuple[dict[str, set[str]], set[str]]:
    stages_to_rules: dict[str, set[str]] = {}
    protected_paths: set[str] = set()
    line_cache: dict[str, list[str]] = {}
    for raw_rule in policy.get("rules") or []:
        if not isinstance(raw_rule, Mapping):
            raise IntelligenceGateError("Every intelligence policy rule must be an object")
        rule_id = str(raw_rule.get("id") or "").strip()
        patterns = [str(item) for item in raw_rule.get("paths") or []]
        stages = [str(item) for item in raw_rule.get("stages") or []]
        line_patterns = [str(item) for item in raw_rule.get("line_patterns") or []]
        if not rule_id or not patterns or not stages:
            raise IntelligenceGateError("Every intelligence rule requires id, paths, and stages")
        try:
            compiled = [re.compile(pattern) for pattern in line_patterns]
        except re.error as exc:
            raise IntelligenceGateError(
                f"Rule {rule_id} has an invalid line pattern: {exc}"
            ) from exc
        for path in changed_paths:
            if not any(fnmatch(path, pattern) for pattern in patterns):
                continue
            if compiled:
                lines = line_cache.setdefault(
                    path, _changed_lines(repository, base, head, path),
                )
                if not any(pattern.search(line) for pattern in compiled for line in lines):
                    continue
            protected_paths.add(path)
            for stage in stages:
                stages_to_rules.setdefault(stage, set()).add(rule_id)
    return stages_to_rules, protected_paths


def _change_fingerprint(
    repository: Path, base: str, head: str, stages_to_rules: Mapping[str, set[str]],
    protected_paths: Iterable[str],
) -> str:
    paths = sorted(set(protected_paths))
    if not paths:
        return ""
    diff = _git(
        repository, "diff", "--binary", "--no-renames", "--no-ext-diff",
        f"{base}...{head}", "--", *paths,
    ).encode("utf-8")
    identity = {
        "schema_version": 1,
        "affected_stages": sorted(stages_to_rules),
        "matched_rules": {
            stage: sorted(rule_ids) for stage, rule_ids in sorted(stages_to_rules.items())
        },
        "protected_paths": paths,
    }
    return hashlib.sha256(_canonical(identity) + b"\n" + diff).hexdigest()


def analyze_change(
    repository: Path, base: str, head: str, policy: Mapping[str, Any],
) -> tuple[list[str], list[str], list[str], str, list[str]]:
    for revision, label in ((base, "base"), (head, "head")):
        _git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}")
        if not revision:
            raise IntelligenceGateError(f"{label} revision is empty")
    changed_paths = _changed_paths(repository, base, head)
    stages_to_rules, protected_paths = _matching_rules(
        policy, repository, base, head, changed_paths,
    )
    fingerprint = _change_fingerprint(
        repository, base, head, stages_to_rules, protected_paths,
    )
    matched_rules = sorted({item for values in stages_to_rules.values() for item in values})
    return (
        sorted(stages_to_rules), sorted(protected_paths), matched_rules,
        fingerprint, changed_paths,
    )


def _read_json_at_ref(repository: Path, revision: str, path: str) -> Mapping[str, Any]:
    return _load_json(_git(repository, "show", f"{revision}:{path}"), path)


def _sha256(value: Any, field_name: str, failures: list[str]) -> str:
    match = SHA256_PATTERN.fullmatch(str(value or "").strip().casefold())
    if not match:
        failures.append(f"{field_name} must be a 64-character SHA-256 value")
        return ""
    return match.group(1)


def _text(
    value: Any, field_name: str, failures: list[str], *, minimum: int = 1,
) -> str:
    rendered = str(value or "").strip()
    if len(rendered) < minimum:
        failures.append(f"{field_name} must contain at least {minimum} characters")
    return rendered


def _timestamp(
    value: Any, field_name: str, failures: list[str], *, max_age_days: int,
    now: datetime,
) -> None:
    rendered = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError:
        failures.append(f"{field_name} must be an ISO-8601 timestamp with a timezone")
        return
    if parsed.tzinfo is None:
        failures.append(f"{field_name} must include a timezone")
        return
    parsed = parsed.astimezone(timezone.utc)
    if parsed > now + timedelta(minutes=5):
        failures.append(f"{field_name} cannot be in the future")
    if parsed < now - timedelta(days=max_age_days):
        failures.append(f"{field_name} is older than {max_age_days} days")


def validate_experiment(
    payload: Mapping[str, Any], *, fingerprint: str, affected_stages: list[str],
    policy: Mapping[str, Any], now: datetime,
) -> list[str]:
    failures: list[str] = []
    if payload.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        failures.append("schema_version must be 1")
    _text(payload.get("experiment_id"), "experiment_id", failures, minimum=6)
    if str(payload.get("change_fingerprint") or "") != fingerprint:
        failures.append("change_fingerprint does not match the protected diff")
    declared_stages = sorted({str(item) for item in payload.get("affected_stages") or []})
    if declared_stages != affected_stages:
        failures.append(
            f"affected_stages must exactly equal {affected_stages}, got {declared_stages}"
        )
    if payload.get("decision") != "pass":
        failures.append("decision must be pass")
    _text(payload.get("why_better"), "why_better", failures, minimum=40)
    limitations = payload.get("limitations")
    if not isinstance(limitations, list) or not limitations or not all(
        isinstance(item, str) and item.strip() for item in limitations
    ):
        failures.append("limitations must be a non-empty array of strings")

    run = payload.get("run")
    if not isinstance(run, Mapping):
        failures.append("run must be an object")
    else:
        _text(run.get("runner"), "run.runner", failures, minimum=3)
        _text(run.get("runner_version"), "run.runner_version", failures)
        _timestamp(
            run.get("completed_at"), "run.completed_at", failures,
            max_age_days=int(policy.get("experiment_max_age_days") or 45), now=now,
        )

    dataset = payload.get("dataset")
    if not isinstance(dataset, Mapping):
        failures.append("dataset must be an object")
        case_count = 0
    else:
        _text(dataset.get("id"), "dataset.id", failures, minimum=3)
        _sha256(dataset.get("fingerprint"), "dataset.fingerprint", failures)
        try:
            case_count = int(dataset.get("case_count"))
        except (TypeError, ValueError):
            case_count = 0
        required_cases = max(
            int((policy.get("minimum_cases_by_stage") or {}).get(stage, 1))
            for stage in affected_stages
        )
        if case_count < required_cases:
            failures.append(
                f"dataset.case_count must be at least {required_cases} for {affected_stages}"
            )
        breakdown = dataset.get("breakdown")
        if not isinstance(breakdown, Mapping) or not breakdown:
            failures.append("dataset.breakdown must document the sample composition")

    for side in ("baseline", "candidate"):
        artifact = payload.get(side)
        if not isinstance(artifact, Mapping):
            failures.append(f"{side} must be an object")
            continue
        _text(artifact.get("name"), f"{side}.name", failures, minimum=2)
        _sha256(artifact.get("artifact_sha256"), f"{side}.artifact_sha256", failures)

    quality_coverage: set[str] = set()
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        failures.append("results must be a non-empty array")
        results = []
    for index, result in enumerate(results):
        prefix = f"results[{index}]"
        if not isinstance(result, Mapping):
            failures.append(f"{prefix} must be an object")
            continue
        stage = str(result.get("stage") or "")
        kind = str(result.get("kind") or "")
        direction = str(result.get("direction") or "")
        _text(result.get("metric"), f"{prefix}.metric", failures, minimum=3)
        if stage not in affected_stages:
            failures.append(f"{prefix}.stage must be one of {affected_stages}")
        if kind not in {"quality", "cost", "latency"}:
            failures.append(f"{prefix}.kind must be quality, cost, or latency")
        if direction not in {"higher_is_better", "lower_is_better"}:
            failures.append(
                f"{prefix}.direction must be higher_is_better or lower_is_better"
            )
        try:
            baseline = float(result.get("baseline"))
            candidate = float(result.get("candidate"))
            allowed = float(result.get("maximum_regression", 0))
        except (TypeError, ValueError):
            failures.append(f"{prefix} baseline, candidate, and maximum_regression must be numeric")
            continue
        if allowed < 0:
            failures.append(f"{prefix}.maximum_regression cannot be negative")
        computed_pass = False
        if direction == "higher_is_better":
            try:
                minimum = float(result.get("minimum"))
            except (TypeError, ValueError):
                failures.append(f"{prefix}.minimum must be numeric")
                continue
            computed_pass = candidate >= minimum and candidate + allowed >= baseline
        elif direction == "lower_is_better":
            try:
                maximum = float(result.get("maximum"))
            except (TypeError, ValueError):
                failures.append(f"{prefix}.maximum must be numeric")
                continue
            computed_pass = candidate <= maximum and candidate <= baseline + allowed
        if result.get("passed") is not True or not computed_pass:
            failures.append(f"{prefix} does not satisfy its declared gate")
        elif kind == "quality" and stage:
            quality_coverage.add(stage)
    missing_quality = sorted(set(affected_stages) - quality_coverage)
    if missing_quality:
        failures.append(f"quality results do not cover affected stages: {missing_quality}")
    return failures


def validate_override(
    payload: Mapping[str, Any], *, fingerprint: str, pr_number: int,
    policy: Mapping[str, Any], now: datetime,
) -> list[str]:
    failures: list[str] = []
    override_policy = policy.get("override")
    if not isinstance(override_policy, Mapping):
        return ["policy.override must be an object"]
    if payload.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        failures.append("schema_version must be 1")
    if str(payload.get("change_fingerprint") or "") != fingerprint:
        failures.append("change_fingerprint does not match the protected diff")
    try:
        declared_pr = int(payload.get("pull_request") or 0)
    except (TypeError, ValueError):
        declared_pr = 0
    if declared_pr != int(pr_number):
        failures.append(f"pull_request must equal {pr_number}")
    approved_by = str(payload.get("approved_by") or "").strip()
    if approved_by not in {str(item) for item in override_policy.get("approvers") or []}:
        failures.append("approved_by is not an allowed intelligence owner")
    if payload.get("acknowledgement") != override_policy.get("acknowledgement"):
        failures.append("acknowledgement does not exactly match the policy text")
    _text(payload.get("reason"), "reason", failures, minimum=40)
    _text(payload.get("risk"), "risk", failures, minimum=40)
    _timestamp(
        payload.get("approved_at"), "approved_at", failures,
        max_age_days=int(override_policy.get("max_age_days") or 7), now=now,
    )
    return failures


def run_gate(
    repository: Path, base: str, head: str, *, pr_number: int = 0,
    policy_path: Path = DEFAULT_POLICY_PATH, now: datetime | None = None,
) -> GateReport:
    repository = repository.resolve()
    policy = load_policy(repository, policy_path)
    affected, protected, rules, fingerprint, changed = analyze_change(
        repository, base, head, policy,
    )
    if not affected:
        return GateReport([], [], [], "", "not_required")
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    experiment_directory = str(policy.get("experiment_directory") or "").rstrip("/")
    experiment_paths = sorted(
        path for path in changed
        if path.startswith(f"{experiment_directory}/") and path.endswith(".json")
    )
    matching_experiments: list[tuple[str, Mapping[str, Any]]] = []
    experiment_errors: list[str] = []
    for path in experiment_paths:
        try:
            payload = _read_json_at_ref(repository, head, path)
        except IntelligenceGateError as exc:
            experiment_errors.append(str(exc))
            continue
        if str(payload.get("change_fingerprint") or "") == fingerprint:
            matching_experiments.append((path, payload))
    if len(matching_experiments) == 1:
        path, payload = matching_experiments[0]
        failures = validate_experiment(
            payload, fingerprint=fingerprint, affected_stages=affected,
            policy=policy, now=current_time,
        )
        if not failures:
            return GateReport(affected, protected, rules, fingerprint, "experiment", path)
        experiment_errors.extend(f"{path}: {failure}" for failure in failures)
    elif len(matching_experiments) > 1:
        experiment_errors.append("More than one experiment matches this protected diff")
    else:
        experiment_errors.append(
            "No changed experiment manifest matches this protected diff fingerprint"
        )

    override_policy = policy.get("override") or {}
    legacy_path = str(override_policy.get("path") or "")
    directory = str(override_policy.get("directory") or "").rstrip("/")
    scoped_path = f"{directory}/pr-{pr_number}.json" if directory and pr_number > 0 else ""
    # The scoped file must be part of this PR. A stale approval already present on
    # the base branch cannot authorize a later change, even with the same fingerprint.
    override_path = scoped_path if scoped_path in changed else legacy_path
    override_errors: list[str] = []
    if override_path and override_path in changed:
        try:
            override_payload = _read_json_at_ref(repository, head, override_path)
            override_errors = validate_override(
                override_payload, fingerprint=fingerprint, pr_number=pr_number,
                policy=policy, now=current_time,
            )
        except IntelligenceGateError as exc:
            override_errors = [str(exc)]
        if not override_errors:
            return GateReport(
                affected, protected, rules, fingerprint, "owner_override", override_path,
                warnings=[
                    "Owner override accepted: this is not evidence that intelligence "
                    "quality improved."
                ],
            )
    else:
        override_errors.append(
            "No changed exact-diff owner override exists at "
            f"{scoped_path or legacy_path}"
        )

    failures = [
        "Protected intelligence changed without a passing exact-diff experiment.",
        *experiment_errors,
        "Owner override was not valid.",
        *override_errors,
    ]
    return GateReport(
        affected, protected, rules, fingerprint, "blocked", failures=failures,
        experiment_directory=experiment_directory,
        override_path_hint=scoped_path or legacy_path,
    )


def _render(report: GateReport) -> str:
    lines = [
        "Intelligence Change Gate",
        f"Result: {'PASS' if report.passed else 'FAIL'}",
        f"Decision: {report.decision}",
    ]
    if report.affected_stages:
        lines.extend([
            f"Affected stages: {', '.join(report.affected_stages)}",
            f"Protected paths: {', '.join(report.protected_paths)}",
            f"Matched rules: {', '.join(report.matched_rules)}",
            f"Change fingerprint: {report.change_fingerprint}",
        ])
    if report.evidence_path:
        lines.append(f"Evidence: {report.evidence_path}")
    if report.warnings:
        lines.extend(["Warnings:", *[f"  - {item}" for item in report.warnings]])
    if report.failures:
        lines.extend(["Failures:", *[f"  - {item}" for item in report.failures]])
        lines.extend([
            "",
            "Normal resolution: if you did not mean to change model behavior, revert the",
            "protected part of the diff. Otherwise run the affected eval and commit a",
            f"matching eval report (manifest) under {report.experiment_directory or 'the experiment directory'}/.",
            "Start one with --scaffold-experiment.",
            "Break-glass resolution: a listed approver reviews this exact fingerprint and",
            f"commits a PR-specific override at {report.override_path_hint or 'the override path'}.",
        ])
    return "\n".join(lines)


def scaffold_experiment(fingerprint: str, stages: list[str]) -> dict[str, Any]:
    """Return an eval report template. Every placeholder must be replaced with real values."""
    placeholder = "0" * 64
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "experiment_id": "REPLACE-describe-the-change-YYYY-MM-DD",
        "change_fingerprint": fingerprint,
        "affected_stages": stages,
        "run": {
            "completed_at": "REPLACE-ISO-8601-with-timezone",
            "runner": "REPLACE-eval-runner-name",
            "runner_version": "REPLACE-commit-or-version",
        },
        "dataset": {
            "id": "REPLACE-golden-dataset-id",
            "fingerprint": placeholder,
            "case_count": 0,
            "breakdown": {"known_positive": 0, "known_negative": 0},
        },
        "baseline": {"name": "REPLACE-baseline", "artifact_sha256": placeholder},
        "candidate": {"name": "REPLACE-candidate", "artifact_sha256": placeholder},
        "results": [
            {
                "stage": stage, "kind": "quality", "metric": "REPLACE-metric",
                "direction": "higher_is_better", "baseline": 0.0, "candidate": 0.0,
                "minimum": 0.0, "maximum_regression": 0.0, "passed": False,
            }
            for stage in stages
        ],
        "decision": "REPLACE-pass",
        "why_better": "REPLACE: what you measured and why it applies to this exact change.",
        "limitations": ["REPLACE: what the golden dataset does not cover."],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--pr-number", type=int, default=0)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--scaffold-experiment", action="store_true",
        help="Print an eval report template with this change's fingerprint and stages.",
    )
    args = parser.parse_args(argv)
    if args.scaffold_experiment:
        try:
            repository = args.repository.resolve()
            policy = load_policy(repository, args.policy)
            affected, _, _, fingerprint, _ = analyze_change(
                repository, args.base, args.head, policy,
            )
        except (IntelligenceGateError, OSError) as exc:
            print(f"Intelligence Change Gate\nResult: FAIL\nFailures:\n  - {exc}")
            return 1
        if not affected:
            print("No protected files changed; no eval report is needed.")
            return 0
        print(json.dumps(scaffold_experiment(fingerprint, affected), indent=2))
        return 0
    try:
        report = run_gate(
            args.repository, args.base, args.head,
            pr_number=args.pr_number, policy_path=args.policy,
        )
    except (IntelligenceGateError, OSError) as exc:
        print(f"Intelligence Change Gate\nResult: FAIL\nFailures:\n  - {exc}")
        return 1
    if args.output:
        args.output.write_text(
            json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(_render(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
