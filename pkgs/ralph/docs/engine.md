# Review engine reference

This reference covers the engine interface and the bundled `current` engine's
output contracts. Start with [agent flow](../agents/README.md) for the pipeline
and [usage](usage.md) for configuration. Service publication and recovery belong
to the [companion guide](../../ralph-service/README.md).

## Engine interface

The CLI or companion supplies a review case and bounded runtime services to
the selected engine and stores its Markdown, private structured data, consumer
exports, run metrics and code/config identity. Only load trusted engine modules
included in the package. Native engine data remains private. Consumers use declared exports;
an omitted export is unavailable, while an empty list is a supported empty
result. They do not derive findings from rendered Markdown. This keeps the
current pipeline's diagnostics available to its own reports without making
those stages a requirement for another engine.

The current engine accepts resolved prompts or prompt file/model settings,
routing, archaeology and blind-alternative controls. Its resolved settings are
fixed for frozen evaluations. Core and engine code determine engine identity;
companion-only website and webhook changes do not change it.

An engine module implements two functions:

```python
configure(config_dict) -> normalized_config_dict
run(case, runtime, normalized_config_dict) -> engine.Output
```

`engine.load(name, config)` calls `configure` and returns an object whose
`run(case, runtime)` checks and returns an `Output`. The output carries
`content` (engine-produced Markdown), `data` (private native data), and
`exports` (supported consumer data), and `status` (`completed` or `failed`,
defaulting to `completed`); generic validation checks the envelope,
not an engine's native schema. Native data stays private by default. The current
report adapter explicitly selects diagnostics only for the declared `current`
engine and native schema version 2, through
[`trace.current_diagnostics`](../ralph/trace.py).
Older results retain their saved review text and declared exports; the current
diagnostic adapter does not interpret their native stage details.

The current engine builds exports from its pipeline result and retains stage
replies and partial attempts in private diagnostics. Finding records preserve
source-verification and judge attribution alongside approved wording.

## Findings and publication

| Check or outcome | What survives |
| --- | --- |
| Malformed discovery response | Discard that stage's candidates and record a limitation; keep other stages. |
| Challenger refutes a candidate | The candidate still reaches the verifier with counterevidence. |
| Challenger fails | Keep every original candidate and record the challenge limitation. |
| Malformed verifier envelope or decision structure | Reject the verifier response; none of its code findings become eligible. |
| Unknown, repeated or omitted candidate IDs | Withhold affected decisions and mark them unresolved; retain independently valid findings. |
| Decisive evidence is missing or mismatched | Withhold that finding and record the missing evidence. |
| Concept objection is unsupported or invalid | Withhold the objection; retain eligible code findings. |
| Judge drops or cannot settle a finding | Withhold it from publication and retain the decision in the trace. |
| Judge fails or returns invalid accounting | Withhold pending findings; no wording fallback. |

The verifier may merge supplied candidates with the same root cause but cannot
introduce code findings. Every decision must account for supplied candidate IDs.
The judge accounts for each eligible finding ID exactly once. It cannot add or
merge IDs, change kind, severity, security assessment or location, or publish
without exact source evidence.

Candidate findings are leads, not votes. Agreement between reviewers gives a
claim no extra weight. A defect needs a reachable trigger and consequence,
checked against the strongest source-based counterexample. Static proof can
suffice; a reproduction is not required.

Review includes tests, CI, build tooling, contrib utilities, and non-security
defects. Security guidance draws on the
[oss-scanner Bitcoin Core threat model](https://github.com/instagibbs/oss-scanner/blob/22ee6a8367b6bd1ef308273a1cdde6aa54e7bb28/projects/bitcoin-core/threat_model.md),
adapted for static PR review. Its scanner-specific exclusions, execution
requirements, and numeric resource thresholds do not restrict Ralph's findings.

The verifier attaches a `security` object with a `classification` of
`potential_vulnerability`, `non_security` or `uncertain`, and a concise `reason`.
This static assessment is independent of kind, severity and publication. An
unsupported security claim does not discard a supported ordinary bug. The
classification appears in structured findings and report diagnostics; it does
not control routing, private triage or publication, or prove exploitability.

Design findings need an established premise and material tradeoff. Suggestions
need a concrete present cost and a supported alternative. A useful design
question can survive even when the implementation is correct. These are
[prompt requirements](../agents/verifier/prompt.md), not mechanical proof checks.

Python enforces structure, candidate accounting, allowed kinds and severities,
nonempty required text, and locations in changed paths that exist on the specified
head/base side with positive integer lines. Published findings also require a
nonempty `source_evidence` array of path, starting line, head/base side, and exact
contiguous full source lines, limited to 150 lines and 12,000 UTF-8 bytes per
excerpt. A final newline is optional. Evidence may cite unchanged files; a claim
about an actual triggering call must include that caller. Python checks the excerpts
against immutable snapshot blobs and withholds mismatches. Matching source text
does not prove the claim or its consequence; the verifier must check those.

The write-verifier can attempt reproduction of supported findings whose
severity is exactly `major`, including design findings. It reports `reproduced`,
`refuted`, or `inconclusive` with runner-recorded command references. These
outcomes are evidence for the judge, not publication decisions. An unavailable
environment, timeout, or impractical experiment does not invalidate a static
proof. A passing test alone does not refute a finding.

See [experiment setup](usage.md#optional-reproduction-experiments) for isolation,
resource limits and the standalone command. The [rendering guide](../final_comment/README.md)
describes comment limits, ordering, Review reports and Run archives.

## Coverage and failures

Tool traces record success, unavailable evidence and invalid requests separately.
Unavailable requested source reads reduce coverage even when the model reports
complete inspection. Discussion and history failures affect optional context;
experiment failures remain separate. Invalid requests stay diagnostic so a typo
can be corrected without declaring source unavailable. A later successful retry
does not erase an earlier unavailable-evidence record. Hosted web calls without
an explicit outcome remain unknown.

Public coverage separates code review, historical context and verification.
Missing advisory history does not reduce code coverage. Expected static-review
scope, such as not running tests, is a scope note rather than missing evidence.
Partial coverage does not discard otherwise valid findings.

Failed source verification or judging marks the run `failed`. The report leads
with a failure notice and retains historical findings for follow-up, without
presenting them as newly verified. The public comment carries a brief
unresolved-history notice. The CLI prints the report and exits with status 1.

## Runtime evidence and limits

By default, model requests use Responses event streams with a 1,800-second
deadline per attempt in both review profiles, including tool calls, final responses and
follow-ups. The deadline includes connecting and reading the response;
heartbeats cannot extend it. Ralph consumes the terminal response, preserving
usage and reasoning, and executes tools only after a completed response.
Ralph still makes at most three attempts per request, so three stalled attempts
can take about 90 minutes.

The initial review input allows 300,000 UTF-8 bytes. Larger patches are replaced
by a complete changed-file inventory with line counts. Commit messages include
their changed paths. Focused stages share an 80,000-byte excerpt allowance
across the paths, so a large fixture cannot displace later functional tests.
Each incomplete excerpt names the next diff line to read. When the path count
leaves too little room for useful excerpts, stages receive the inventory and
must inspect their assigned paths with `read_diff`. Excerpts do not establish
review coverage.
File reads return up to 300 lines and 12,000 UTF-8 bytes per call. Published
source evidence uses the smaller limit in [findings and publication](#findings-and-publication).

Code tools find paths, read head/base files, read diffs and search code.
`read_pr_commit` pages through a changed file in a commit from the pinned review
range, including intermediate changes absent from the final net diff. A merge
commit is compared with its first parent. Reviewers finish against the combined
head, since separately sound commits can interact. The full
tool set also reads base history and, when a live discussion reader is supplied,
other PR discussions. Current PR discussion is excluded from code discovery.
In frozen runs, discussion tools are available only to the archaeologist and
verifier. These stages can read current discussion and original upstream review
threads from the supplied evidence. Frozen evaluations replay only
captured research evidence and disable live discussion or web fallback. If a
research call is missing from the frozen manifest, the tool returns an
unavailable-evidence message and the model continues from code and frozen input.

Each agent's [config.toml](../agents/) defines its tool allowances and request
timeouts. [Contributing](../CONTRIBUTING.md#settings) explains their units and
profile precedence. Hosted web actions count toward the stage limit, but the
per-response search allowance is advisory. At limits, a final turn uses the
collected evidence and must preserve uncertainty.

Script, symbol_usage, commit_flow, independent, challenger, both adversarial
passes, verifier and judge require a first inspection. Claude uses automatic
tool selection; Ralph checks that inspection occurred.

## Concept and history

Optional archaeology researches the proposal's goal, benefit, prior attempts
and alternatives. It runs alongside discovery and supplies context only to the
verifier. Historical claims need source links, and earlier objections must be
checked for later answers. Promised follow-ups do not count as delivered benefits.
See the [archaeologist prompt](../agents/archaeologist/prompt.md) for review criteria.

Its structured proposal records `goal`, an `assessment` of `worth_pursuing`,
`needs_motivation`, `rework_approach` or `not_worth_pursuing`, and an advisory
`proposed_review` of `continue`, `would_stop` or `undetermined`, with a
`review_reason`. None of these recommendations stops selected code-review stages.

After a valid concept candidate, enabled blind alternatives receive neutral
problem, goal and baseline inputs with base-only code tools. Their result goes
to the verifier, never to discovery. Backport reviews skip this pass.

The verifier checks the premises of conceptual feedback and may accept an
actionable objection or one evidence-backed optional alternative with stated
tradeoffs. Conceptual feedback has no finding ID, severity or artificial source
location. The judge may approve it without any eligible code findings; failure
produces no fallback wording. Optional alternatives render from verifier-approved
fields. Favorable assessments and rejected alternatives remain in diagnostics.

Reports distinguish completed research from published assessments and retain
both proposed and verified recommendations. These counts measure output, not
recommendation quality. Use [frozen evaluation](../../../modules/ralph/evaluation.md)
for repeatable comparisons; a live rerun can see newer discussion.

## Backport reviews

The current engine reviews backports for upstream fidelity and correctness on the
release branch. A `Backport` label, a `Rebased-From:` commit trailer, or explicit
backport intent targeting a release branch selects this mode. A release branch
alone does not. Domain risk floors and the existing finding verification gates
still apply; blind alternatives are skipped.

The evidence collector maps original commits to backported commits, compares each
pair, inventories claimed source PRs and untagged release preparation, and checks
release-note references. Reviewers assess missing dependencies, intentional partial
backports, conflict resolutions and interactions. Mechanical differences and
missing release-note references are leads, not automatic defects.

Reports include a separately verified backport assessment with pinned base/head
revisions, evidence, required fixes and limitations. Unavailable originals,
unverified source membership and incomplete review prevent a `Faithful` verdict.
Normal code findings still require valid source evidence and changed-line anchors.
The upstream tools read only resolved original commits and their parents, under
existing tool budgets. Major findings can use the same optional isolated
experiments as ordinary reviews.

The local CLI detects commit trailers and uses available local objects without
network access. It reports unverified source PR membership as a limitation. The
companion service acquires source PR metadata and pins upstream history before
calling the engine.

## Author feedback on subsequent reviews

Re-reviews may carry previous published findings from the same repository and
PR. Discovery reads the new code without author replies. A bounded feedback
pass maps explicit replies to earlier findings and current candidates; the
verifier checks the technical claims against current source.

An author saying "addressing elsewhere" or "won't fix" does not resolve a
finding. Fixed and invalid decisions require source evidence. Historical
concerns must be verified at the current head before joining published findings,
and cannot restore a rejected current candidate. Suppressing a repeated upstream
finding requires an explicit author response and base/head evidence that the PR
does not introduce or worsen it.

Findings retain their original identity and assessment history. Missing or
invalid reconciliation leaves affected entries unresolved; malformed rows do
not discard valid siblings. Confirmed duplicates share one displayed concern
while retaining each report's provenance and status. Similar wording or a
shared current candidate does not establish duplication. Existing duplicate
links survive unavailable verification, and unanswered active reports remain
unresolved.

The structured `review_memory` export is the source for later reviews. Consumers
must account for unresolved history as well as newly published findings. First
reviews do not invoke the feedback pass. Frozen cases capture the feedback
packet and its identity checks. See [comment and report rendering](../final_comment/README.md)
for display rules and the [service guide](../../ralph-service/README.md#pr-local-author-feedback)
for evidence acquisition and reuse.

Implementation: [engine.py](../ralph/engine.py),
[pipeline.py](../ralph/pipeline.py), [protocol.py](../ralph/protocol.py),
[model.py](../ralph/model.py) and [config.py](../ralph/config.py).
