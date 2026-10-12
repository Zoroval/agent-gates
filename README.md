# agent-gates

**Eval gates for agentic coding, from [Zoroval](https://zoroval.com).**

[![tests](https://github.com/Zoroval/agent-gates/actions/workflows/test.yml/badge.svg)](https://github.com/Zoroval/agent-gates/actions/workflows/test.yml)

Coding agents like Claude Code and Codex write more code than any team can review, and the riskiest part of that code is the part a diff cannot explain: prompts, context builders, model settings and retrieval code, which change what your LLM concludes. This repository is an agent skill that sets up an **eval gate** in your CI. A pull request that touches one of those files cannot merge until an LLM eval of that exact change passes, or until an owner signs a visible break-glass override.

This is a version of the gate that Zoroval runs in its own CI, with our internal paths and messages removed. The full story of why we built it, and the LLMOps pipeline around it, is on our blog: **[Code review can't keep up with agents. Gates can.](https://zoroval.com/blog/llm-eval-gates-agentic-coding)**

## Why Zoroval built this

Zoroval uses LLM-as-a-judge to find failure modes in the conversations between a company's AI agent and its users. One day our judge started missing failures that a person had already confirmed. The model had not changed, and neither had the prompt, so no eval ran. The root cause was the **context builder**, the code that assembles what the model sees, and nothing treated it as part of the intelligence system.

Code tests check the shape of an LLM's output, not its judgment. With coding agents touching that code all the time, we needed a gate that knows which files can change model behavior and refuses to merge them on green tests alone. This is that gate.

## What the gate does

1. **You declare what is protected.** `.github/intelligence-policy.json` lists the paths that can change model behavior, grouped by product stage. For shared files, `line_patterns` limit the gate to model-related lines.
2. **It fingerprints the exact change.** On each PR it hashes the diff of only the protected files, together with the affected stages and rules (SHA-256).
3. **The PR must carry an eval report for that fingerprint.** A small JSON manifest under `evals/experiments/` names the golden dataset, baseline, candidate and a quality result for every affected stage. The gate recomputes pass and fail from the numbers and never trusts `"passed": true`.
4. **Evidence goes stale by itself.** Edit a protected file after the eval, and the fingerprint changes. Docs and tests do not affect it.
5. **Break-glass overrides are visible.** An approver can accept a trade-off without an eval, for example a deliberate shift from precision to recall. The override is per PR, names the fingerprint, needs a reason and a risk, expires, and shows as `owner_override`, never as a pass.
6. **Failures talk to coding agents.** The output names the stages, files, rules and fingerprint, so the agent can revert an accidental change or produce the evidence itself.

```
Intelligence Change Gate
Result: FAIL
Decision: blocked
Affected stages: support_reply
Protected paths: src/support/context_builder.py
Matched rules: support-reply-prompt
Change fingerprint: 3f9c…e21a
Failures:
  - Protected intelligence changed without a passing exact-diff experiment.
  - No changed experiment manifest matches this protected diff fingerprint
```

The gate uses only the Python standard library, so it runs before your dependencies install.

## Install the skill

**With the skills CLI:**

```bash
npx skills add Zoroval/agent-gates
```

**Claude Code, by hand:** copy `skills/agent-gates` to `.claude/skills/agent-gates` in your repository, or to `~/.claude/skills/agent-gates` for all your projects.

**Codex and other agents:** copy `skills/agent-gates` to the skills folder your agent reads, or point the agent at `skills/agent-gates/SKILL.md`.

Then ask your coding agent:

> Set up eval gates for this repository.

The skill finds the files that can change model behavior, including context builders, and asks you to confirm a policy. It then adds the gate, the CI workflow, an eval report template, the `AGENTS.md` rules and the `CODEOWNERS` entries, and tests the gate on a throwaway branch. It does not push or open a PR unless you ask.

## An eval report

One eval report per protected change, under `evals/experiments/`. Start one with `--scaffold-experiment`, which fills in the fingerprint and stages:

```json
{
  "schema_version": 1,
  "experiment_id": "monitor-context-builder-2026-10-02",
  "change_fingerprint": "<the 64-character fingerprint printed by the gate>",
  "affected_stages": ["monitoring"],
  "run": { "completed_at": "2026-10-02T09:30:00Z", "runner": "offline-replay", "runner_version": "<commit>" },
  "dataset": {
    "id": "private-golden-dataset",
    "fingerprint": "<sha256 of the private dataset>",
    "case_count": 50,
    "breakdown": { "known_positive": 20, "known_negative": 30 }
  },
  "baseline":  { "name": "context builder at main",  "artifact_sha256": "<sha256>" },
  "candidate": { "name": "context builder in this PR", "artifact_sha256": "<sha256>" },
  "results": [
    { "stage": "monitoring", "kind": "quality", "metric": "verdict_agreement",
      "direction": "higher_is_better", "baseline": 0.94, "candidate": 0.96,
      "minimum": 0.9, "maximum_regression": 0.0, "passed": true }
  ],
  "decision": "pass",
  "why_better": "Same verdicts on all 20 known positives. One fewer false alarm on the 30 known negatives (47 to 48 correct of 50).",
  "limitations": ["Only one failure mode is in the sample."]
}
```

Raw data stays private, because the report only points to it by SHA-256.

## What the gate checks, and what it cannot

The gate checks that a protected file changed, that an eval report in the PR matches the exact protected diff, that every affected stage has a quality result, that the sample meets the policy's minimum size, that the run is recent, and that each result meets the thresholds written in the report.

It cannot check that the eval ran or that the numbers are real, because CI never sees your private data. It cannot check that the thresholds are right, because the report's author writes them. It cannot check that your golden dataset covers the failure modes that matter, and it is blind to files the policy does not list. A person must still review each eval report, and the next section makes that review required.

## Make it enforceable

A `CODEOWNERS` file only requests reviews. In a branch protection rule or a ruleset for each protected branch:

- turn on **Require a pull request before merging** and **Require review from Code Owners**;
- make the `intelligence-gate` check a **required status check**;
- keep `/.github/CODEOWNERS` itself owned by the repository owner (the skill adds this line);
- remember that GitHub reads `CODEOWNERS` from the PR's base branch, and code owners need write access.

**A trap for small teams:** GitHub does not let a PR author approve their own PR, so if your coding agents open PRs from your account, you cannot give the code-owner approval. Give the agents their own account. See GitHub's [About code owners](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners).

Keep the policy narrow, too. A gate that fires on every PR gets overridden on every PR.

## Start with a golden dataset

Every eval gate depends on a golden dataset: real traces where someone confirmed a failure, and real traces where they confirmed there was none. A dataset with only positive examples measures recall but never precision, so a model that says "yes" too often will pass it. If you do not have a golden dataset yet, start there, before the gate.

## About Zoroval

[Zoroval](https://zoroval.com) helps teams whose users talk to their AI know whether each change made their agent better or worse.

- **Your experts define the failures.** Domain experts review real production traces and describe what went wrong in their own words. Zoroval groups those notes into named failure modes, so the categories come from what people actually see, not from a generic list.
- **Each failure mode becomes a monitor.** Zoroval turns every failure mode into an LLM-as-a-judge monitor with a focused context built for that failure, and runs it on your live traffic. The method is in our post on [finding one failure mode in a 230,000 character trace](https://zoroval.com/blog/judging-failure-modes-in-long-agent-traces/).
- **Each change gets an answer.** When you change a prompt, a model or a context builder, you can see whether the failures your users hit went up or down.
- **The labeled traces become your golden dataset.** The same expert-confirmed traces give the eval gate in this repository something real to measure.

This repository is how we keep our own product honest. Zoroval is how we help you do the same for yours. If your team runs an AI agent that talks to users, [we would like to talk](mailto:hello@zoroval.com).

- Website: [zoroval.com](https://zoroval.com)
- Blog: [zoroval.com/blog](https://zoroval.com/blog/)
- LinkedIn: [Zoroval](https://www.linkedin.com/company/zoroval/)

## Files

```
skills/agent-gates/
  SKILL.md                                  the procedure the coding agent follows
  references/finding-protected-surfaces.md  how to find prompt, context builder, model and retrieval code
  assets/validate_intelligence_change.py    the gate (a version of the one Zoroval runs)
  assets/intelligence-gate.yml              GitHub Actions workflow
  assets/intelligence-policy.example.json   policy template
  assets/experiment.example.json            eval report template
  assets/override.example.json              break-glass override template
  assets/AGENTS.snippet.md                  rules for your coding agents
  assets/CODEOWNERS.snippet                 owner protection for the gate
tests/test_gate.py                          gate tests (python -m unittest discover tests)
```

## License

MIT. Copyright (c) 2026 Zoroval.
