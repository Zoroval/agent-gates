---
name: agent-gates
description: Set up an eval gate (Intelligence Change Gate) in a repository that ships LLM features. The gate blocks any PR that changes model behavior (prompts, context builders, model choice, reasoning settings, output schemas, retrieval, LLM-as-a-judge prompts, model-written reports) unless the PR carries a passing eval report bound to a fingerprint of the exact change, or a visible, expiring break-glass override. Use when the user asks to set up LLM gates, eval gates, prompt-change checks, "gates for agentic coding", or protect a repo from unreviewed model-behavior changes. Also use when a gate in the repo fails and the user asks how to pass it.
---

# Agent gates

Coding agents write more code than people can read. This skill moves review from lines to evidence: people review a small policy file and each eval report, and the gate checks every PR against the policy. It is a version of the eval gate Zoroval (https://zoroval.com) runs in its own CI; background in https://zoroval.com/blog/llm-eval-gates-agentic-coding

Read the whole skill before you change any file. Ask the user before you write the policy, because the policy decides what every later PR must prove.

## What you will add

| File | Purpose |
|---|---|
| `.github/intelligence-policy.json` | Which paths can change model behavior, grouped by stage |
| `tools/validate_intelligence_change.py` | The gate. Python standard library only. |
| `.github/workflows/intelligence-gate.yml` | Runs the gate on every pull request |
| `evals/experiments/` | Eval reports (JSON manifests), one per protected change |
| `evals/experiment.example.json` | Template for an eval report |
| `.github/intelligence-overrides/` | Per-PR break-glass overrides |
| `AGENTS.md` section | Tells agents what to do when the gate fails |
| `CODEOWNERS` entries | Only the owner can change the gate and its evidence |

Use the files in `assets/` as the source. Adjust paths to the repository's layout. If the repository already has `tools/`, `scripts/` or `bin/`, use the existing folder.

## Step 1: Find the protected surfaces

Follow `references/finding-protected-surfaces.md`. Produce a table of candidate paths with:

- the path or glob;
- the stage it affects (use product words, for example `support_reply`, `retrieval`, `judge`, `summary`);
- why it can change model behavior;
- whether only some lines matter (then propose `line_patterns`).

Be specific. Do not protect `src/**`. A gate that fires on every PR gets overridden on every PR, and then it protects nothing.

Show the table to the user. Ask them to confirm, remove or add rows. Do not continue until they confirm.

## Step 2: Write the policy

Start from `assets/intelligence-policy.example.json`.

- One rule per coherent surface. Give each rule a stable `id`.
- A rule can affect more than one stage, for example a shared prompt transport.
- Use `line_patterns` (Python regular expressions) for shared files such as settings, so the gate only fires when a model-related line changes.
- Set `minimum_cases_by_stage`. Ask the user. If they have no opinion, use 10.
- Set `override.approvers` to the GitHub usernames the user names. Do not guess them.
- Keep `override.max_age_days` at 7 unless the user asks otherwise.

## Step 3: Add the gate and the workflow

1. Copy `assets/validate_intelligence_change.py` to the tools folder.
2. Copy `assets/intelligence-gate.yml` to `.github/workflows/`. Fix the script path if you changed it. Keep `fetch-depth: 0`, because the gate needs the merge base.
3. Copy `assets/experiment.example.json` to the experiment directory's parent.
4. Create the experiment directory and the overrides directory with a `.gitkeep` file each.

The gate runs before any dependency install. Do not add imports outside the standard library.

## Step 4: Tell the agents

Append `assets/AGENTS.snippet.md` to the repository's `AGENTS.md` (create it if missing). If the repository also has `CLAUDE.md`, add one line there that points to the `AGENTS.md` section. Replace the placeholder paths and approver names.

## Step 5: Protect the gate

Append `assets/CODEOWNERS.snippet` to `.github/CODEOWNERS` (create it if missing) with the user's approvers. Keep the line that makes `/.github/CODEOWNERS` owned by the approver.

A `CODEOWNERS` file alone enforces nothing. Tell the user to do these in a branch protection rule or ruleset for each protected branch, because you cannot do them from the repository:

- turn on "Require a pull request before merging" and "Require review from Code Owners";
- make the `intelligence-gate` check required;
- give every code owner write access.

Also warn them: GitHub does not let a PR author approve their own PR. If agents open PRs from the approver's account, the approver cannot give the code-owner review. Agents need their own account.

Tell the user that the gate checks a manifest's internal consistency, not whether the experiment ran or whether its thresholds are sensible. The code-owner review of each manifest is where a person checks the sample, the thresholds and the limits.

## Step 6: Test it

On a new local branch:

1. Change one line in a protected file. Run:
   `python tools/validate_intelligence_change.py --base <default-branch> --head HEAD`
   Expect `Result: FAIL` and a fingerprint.
2. Run with `--scaffold-experiment` to print a manifest with the fingerprint and stages filled in. Do not commit it as a real experiment. Confirm the gate checks it (it will fail on placeholder values; that is correct).
3. Change only a test or a doc file. Expect `Decision: not_required`.
4. Delete the test branch.

Report the results to the user. Do not open a PR or push unless the user asks.

## When the gate fails on a PR

Read the gate output. It names the affected stages, protected paths, matched rules and the change fingerprint. Then, in this order:

1. **Did you mean to change model behavior?** If not, revert the protected part of the diff. Run the gate again. Often this is the whole fix.
2. **If you did:** run an eval of the baseline and the candidate on the affected stages against the team's golden dataset (known positive and known negative cases). Use the repository's eval harness if there is one. Write an eval report under the experiment directory, starting from `--scaffold-experiment`. Fill in real numbers. Never set `passed: true` by hand when the numbers do not satisfy the thresholds; the gate recomputes it.
3. **If you cannot run an experiment:** stop and tell the user. Show them the fingerprint, the stages and the risk. Only a listed approver can create an override, and only for this PR. Never create an override file yourself unless the user explicitly authorizes it with the PR number.

Any later edit to a protected file changes the fingerprint. Evidence for an older fingerprint will not pass. Run the experiment on the final version of the change.

## Rules

- Never weaken the policy, the gate or the workflow to make a PR pass.
- Never commit raw customer data, raw traces or provider keys in a manifest. Reference private artifacts by SHA-256.
- An override is a recorded decision, not evidence. Never describe it as a pass.
