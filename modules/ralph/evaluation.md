# Frozen review evaluation

`ralph-evaluate` is installed by `packages.<system>.ralph-service`, alongside
the webhook service and follow-up command. It imports the same core runner as
the standalone `ralph review` command.

The frozen runner is for local comparison of review configurations. Capture
stores the PR text, Git object IDs, prompt/model configuration, bounded current
discussion, optional prior discussion calls, and hashes. Replay reads those
frozen inputs and writes private run artifacts. It does not fall back to live
discussion or web research. Missing research evidence becomes an unavailable
tool result, so the review continues from code and the frozen input. The
`summarize` command reads saved artifacts offline; it does not call Forgejo, Git
remotes, or a model.

Captures pin the selected engine and its resolved configuration as well as the
repository and prompt inputs. `capture` accepts `--engine` and
`--engine-config PATH` (a JSON object). `run` uses the captured selection by
default; pass the same options to override it for a comparison. Use
`--agents-dir DIR` to load another complete directory of agent prompts and TOML
settings. The current-engine agent, routing and blind-alternative switches
cannot be combined with another engine. Configuration changes produce a distinct
effective configuration group. The capture schema has a hard
cutover: recapture manifests created before engine identity was added. Current
captures use manifest schema version 4. Recapture pre-split manifests. Core and
engine hashes are separate from companion presentation code, and captures record
the resolved archaeology setting. Frozen evidence never falls back to live reads.
Follow-up prompt and model overrides are separate companion settings.

Agent prompts, models and tool limits live under
[pkgs/ralph/agents](../../pkgs/ralph/agents/); comment templates live under
[pkgs/ralph/final_comment](../../pkgs/ralph/final_comment/). Validate contributor
edits with `python3 pkgs/ralph/review.py check-agents` before paying for a replay.
Add `--agents-dir DIR` to validate an alternate directory. Frozen replay uses
its captured prompts and settings until you explicitly select an override.

An engine owns its Markdown and structured data. Reports and other consumers
use only exports the engine declares; an absent export is unavailable, while an
empty supported export means no items. They do not infer findings by parsing
review text. Other engines need not provide current-engine stage diagnostics.

```sh
ralph-evaluate summarize ./results
```

The summary groups runs by `effective_config_sha256`. For each group it counts
runs, failures, partial coverage, budget exhaustion, labels present, verifier
decisions, accepted findings, archaeology completion, published conceptual
concerns, candidate and verified review recommendations, known estimated model
spend, unknown usage indicators, elapsed time, and per-stage source attribution.

The cost fields intentionally separate attempt totals from per-run averages.
`cost_usd.total` is the sum of priced token usage saved in the run artifacts.
Unknown usage is excluded from that subtotal and counted separately. Accepted
finding counts mean verifier-published findings. Conceptual concern counts mean
the verifier published a concept assessment. Neither count is a human accuracy
or recall measurement.

## Case selection

Use frozen cases that represent the decisions the bot has to make in real
review. Include both known bugs and clean changes. A useful set covers:

- consensus and script behavior
- P2P, relay, and denial-of-service risk
- wallet funds, signing, descriptors, fees, and privacy
- C++ lifetime, lock ordering, callbacks, and shutdown
- persistence, migration, restart, reorg, rescan, and recovery
- RPC, CLI, configuration, error, and result contracts
- build, portability, dependency, and toolchain changes
- test-only changes and clean refactors

Freeze the reviewed head before a later fix. Use `--research-cutoff` when later
discussion should not be visible, and use `--research-requests-json` for exact
prior discussion calls that the replay may need. The requests file is a JSON
list of tool calls using `search_discussions`, `read_discussion`,
`read_current_pr_discussion` or `read_github_discussion`. Keep expected findings
and labels out of reviewer inputs.

## Human scoring

Treat labels as local notes for human comparison. Current labels are arbitrary;
the runner does not know whether a published finding matches a label, and the
summary must not be read as recall.

Score final comments with blinded judgment. The scorer should not know which
configuration produced each comment. For each case, record unique valid
findings, severity, actionability, false positives, missed known labels, useful
design criticism, false proposed stops, and useful code findings that appeared
after a candidate or verified `would_stop` recommendation. Review a sample of
dropped and unresolved verifier decisions, especially consequential claims, so
confident false drops are visible.

For known-bug cases, count a finding as valid when it identifies the changed
behavior, reachable precondition, broken invariant, and consequence well enough
for a maintainer to act. For clean cases, count unsupported defect claims as
false positives. A proposed stop is false when the PR still deserved code review
or later stages found useful code findings. Keep design feedback and concept
assessment quality separate from correctness recall. Public concept feedback
should contain only supported, actionable objections. The separate "Alternatives"
section may contain at most one independently verified suggestion that improves
a meaningful dimension under the PR's requirements, with its tradeoffs stated.
It can appear even when the PR's overall approach is sound. Check for duplicated
design findings and unnecessary prose. Score favorable assessments and rejected
or inconclusive alternatives from the detailed trace, where they are retained.

## Comparisons

Run a baseline first, then change one factor at a time. Compare baseline runs
against selective routing, prompt profiles, model profile changes, reasoning
effort changes, and the `--no-blind-alternatives` ablation by their
`effective_config_sha256` groups. Blind alternatives are on by default after a
valid concept candidate. The blind run uses the existing archaeologist model,
base-only code tools, and neutral problem, goal and baseline inputs; its result
goes to the final verifier, not to discovery. Use the summary for cost, latency,
coverage, failure, review recommendation and verifier disposition counts. Use
the human scoring sheet for recall, false positives, missed labels, false drops
and false proposed stops.

Useful comparison fields are unique valid findings, known-defect recall by
consequence, false positives per PR, verifier false drops, unresolved claims,
dollars per PR, dollars per unique useful finding, p95 cost, p95 wall time,
budget exhaustion, failed attempts, candidates lost when a stage cannot finish,
and useful downstream findings after proposed stops.

Repeat promising configurations on a smaller set to check variation before
running held-out cases. Add a specialist only when it finds useful issues that
the existing stages miss often enough to justify the extra spend.
