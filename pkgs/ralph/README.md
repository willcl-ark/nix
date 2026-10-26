# ralph local review

Ralph loads a selected local review engine. The default `current` engine performs
static reviews of committed Bitcoin Core changes, discovers candidate findings in independent
passes, checks them against repository evidence, and returns verified findings
and a separately verified conceptual assessment. It does not build or execute
reviewed code. The engine owns its Markdown and structured result; other engines need
not use the stages, diagnostics or exports described below. Operators can load
only trusted engine modules included in the package. Engines own their output
style and engine-specific checks; current-engine tests and diagnostics do not
prescribe another engine's review format.


`packages.<system>.ralph` installs only the `ralph` review command. The
[companion package](../ralph-service/README.md) supplies the webhook service,
frozen evaluator, reports, statistics and follow-up scheduler.

```sh
ralph review topic /path/to/repo \
  --base refs/remotes/origin/master \
  --openai-key-file /path/to/openai-key \
  --output /tmp/topic-review.json

ralph review 'HEAD^..HEAD' /path/to/repo \
  --openai-key-file /path/to/openai-key
```

Branch mode requires an explicit `--base` and compares the unique merge base
with the target. An explicit `BASE..HEAD` range compares those exact committed
trees, including diverged endpoints. Both endpoints are required. Working-tree
and index changes, root commits, and triple-dot shorthand are unsupported.
Ralph resolves revisions once, pins their objects until the run finishes, and
reads blobs without checkout or index changes. It never fetches Git objects or
calls a forge. Missing objects or history fail before model requests.

Stdout contains review Markdown. Progress and errors go to stderr. `--output`
writes a private versioned JSON artifact containing engine identity, base/head,
merge base, actual diff base, native data, supported exports, usage and status.
PPQ credentials are optional for local review. Supplying `--ppq-key-file` also
requires an explicit `--ppq-review-budget-usd` allowance. The OpenAI default is
USD 1.00. Use `--help` for budget and trusted local engine options.

Archaeology defaults off for local changes. `--context FILE` can supply versioned
review context and optional source metadata, for example:

```json
{
  "schema_version": 1,
  "title": "Review title",
  "rationale": "Problem and intended change",
  "source": {
    "kind": "pull_request",
    "url": "https://git.example.org/owner/repo/pulls/123",
    "number": 123
  }
}
```

The `source` field is optional. A PR source records its actual URL and number. `--research-evidence FILE` supplies captured discussion evidence;
core never retrieves discussions itself. `--archaeology auto|on|off` resolves to
a boolean before engine identity is recorded. Forced enabling requires usable
PR context and discussion evidence. Disabled archaeology and blind alternatives
make no model requests and do not reduce ordinary code coverage. Hosted web
permission is separate from discussion access.

Custom main-review model maps must remove `addressed_findings`, and custom audit
directories no longer need `addressed_findings.md`. Follow-up prompt and model
settings belong to the companion and are configured separately.

## Engine boundary

The CLI or companion supplies a review case and bounded runtime services to the selected
engine. It stores the engine-produced Markdown, native structured data,
optional consumer exports, common run metrics and the engine's code/config
identity. Native engine data remains private. Consumers use declared exports;
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
`exports` (supported consumer data); generic validation checks the envelope,
not an engine's native schema. Native data stays private by default. The current
report adapter explicitly selects diagnostics only for the declared `current`
engine and native schema version 2, through
[`trace.current_diagnostics`](ralph/trace.py).
Older results retain their saved review text and declared exports; the current
diagnostic adapter does not interpret their native stage details.

Inside the current engine, [pipeline.py](ralph/pipeline.py) keeps scheduling,
stage requests, verification and editing in separate functions. It returns a
`ReviewResult` containing Markdown, final finding records and the verified
concept concern. [current_engine.py](ralph/current_engine.py) builds exports
from that result; diagnostic dictionaries retain stage details and partial
attempts. Finding records preserve original and presented wording, and exports
keep verification order even if the editor reorders the prose.

The diagrams and implementation details below describe the `current` engine.

These diagrams describe the checked-in defaults. [profiles.py](ralph/profiles.py) defines each review profile.
Callers can replace the complete model map with `--models-json`.
The [NixOS module documentation](../../modules/ralph/README.md)
covers deployment and options. Mermaid diagrams render in supporting Markdown
viewers; their source remains readable in a plain text editor.

## Audit selection

Enabled routing combines the router's proposal with mandatory path coverage.

```mermaid
flowchart LR
    input["Patch + changed paths"] --> router["Router proposal<br/>or conservative fallback"]
    router --> rules["Required audits<br/>+ risk floor"]
    rules --> plan["Selected review plan"]
```

| Selection or escalation | Result |
| --- | --- |
| Enabled routing | Add mandatory path audits and profiles; never lower the path-based risk floor. |
| Explicit full mode | Select every audit and profile, including script. |
| Shadow mode | Record the routing proposal, then select every audit and profile, including script. This also applies after routing fallback. |
| Invalid or failed routing, missing context, or oversized patch | Use the sensitive general plan with all profiles, excluding script. Full and shadow modes still include script. Budget exhaustion stops the attempt. |
| Overview requests sensitive review from a lower tier | Add all general audits and profiles, excluding script. |
| A focused audit requests sensitive review from a lower tier | Add adversarial, state and concurrency; keep selected profiles, or use all profiles if none were selected. |

The router can add coverage but cannot remove path-mandated coverage. A sensitive
tier always adds adversarial review, even when no domain profile is selected.
Domain profiles extend the adversarial prompt; they are not separate model calls.
They are distinct from the `default` and `high` review settings profiles.

| Changed paths, excluding tests where stated | Mandatory coverage / tier floor |
| --- | --- |
| Documentation or tests only | Routine floor; tests audit for test changes |
| Any non-documentation path, including tests and recognized build files | dead_code audit |
| Production code, excluding docs, tests and recognized build paths | Standard floor; tests and design audits |
| Consensus, validation, script, chainstate, serialization and related paths | Sensitive; consensus profile |
| Wallet paths | Sensitive; wallet profile |
| Network, policy, mempool and related paths | Sensitive; p2p profile |
| Synchronization, scheduler, queues, validation callbacks, thread utilities | Sensitive; concurrency audit |
| Databases, indexes, wallet storage, chainstate and block storage | Sensitive; state audit |
| RPC, CLI, HTTP, REST, initialization and argument handling | Standard; public_contract audit |
| Recognized build paths, including CMake, depends, Guix, CI and Cargo/flake locks | Standard; build audit |
| Crypto and secp256k1 paths | Sensitive floor |

These are summaries of the regex rules in [routing.py](ralph/routing.py).
Documentation is excluded from directory-based coverage; `CMakeLists.txt` and
`requirements.txt` are configuration despite their suffix. CI scripts under
`ci/test/` retain both build and test audits. Rules overlap: wallet RPC and wallet build files still
have a sensitive floor. The router can add audits, profiles or a higher tier
based on behavior, including authentication, untrusted inputs, secrets,
dependency provenance and release integrity. Empty audit and profile lists are
valid when no coverage applies. Routine does not mean no review.

## Script and transaction-policy specialist

The `script` audit is selected by the cheap router only for substantive changes
in script execution or flags, signatures and sighash commitments, witness or
annex rules, spend-type acceptance, or transaction validity and fee-bumping
constraints. Filenames and a sensitive tier do not select it automatically.
Documentation, test-only changes, mechanical refactors, logging, metrics and
unrelated work in validation or policy files skip it. Generic missing context,
routing failures and overview escalation do not add it. Explicit `full` and
`shadow` modes still request every audit, including `script`.

Its [prompt](audits/script.md) checks consensus versus policy, complete base/head
functions and relevant spend-type combinations. It distinguishes signed
participants from inputs such as P2A anchors, and tests the premise of a new
transaction-wide rule against existing exceptions. The PR #36466 all-input annex
condition is a motivating case, rather than a reason to run this stage on every
transaction-related patch.

The default is `gpt-6.1-sol`, high reasoning, 25,000 generated tokens per response
including reasoning, and 48 total tool calls. These are initial settings, not a
measured optimum. The specialist sees original PR evidence and repository code,
not other findings or review discussion. Code tools inspect base/head, diffs,
callers and tests; history tools settle specific contracts. It must inspect code
before finishing and prioritize material functions over reporting them unread.

When selected it runs before any enabled concept research and other discovery, sharing the
existing OpenAI budget and protecting verifier headroom and its own final turn.
It has no additional spending allowance; inadequate review or monthly budget
still produces an explicit incomplete stage. Candidates go through the existing
verifier, and named discovery gaps are passed to the verifier for targeted
inspection. Remaining gaps stay visible in coverage. Stage stats include
selection, execution, coverage, costs and sole/shared accepted findings.
Custom model maps must include `script`; there is no fallback to an old map.

## Stages, models and evidence

Read each row from left to right, then continue with the next row. Arrows show
execution order, not shared findings. Dashed boxes run only when selected or
enabled. Model names and limits are listed in the table below.

**1. Select coverage and gather optional context**

```mermaid
flowchart LR
    route["Route"] --> script["Script audit<br/>if selected"]
    script --> history["Archaeology<br/>if enabled"]
    history --> alternatives["Blind alternatives<br/>if enabled + valid concept"]
    classDef optional stroke-dasharray: 5 4
    class script,history,alternatives optional
```

**2. Discover independent candidates**

```mermaid
flowchart LR
    overview["Independent<br/>overview"] --> adversarial["Adversarial passes<br/>Sol + Gemini<br/>in parallel when selected"]
    adversarial --> focused["Selected focused audits<br/>run sequentially"]
    focused --> pool["Candidate pool"]
    classDef optional stroke-dasharray: 5 4
    class adversarial,focused optional
```

Focused audit order: concurrency, state, public_contract, build, tests,
dead_code, design. Discovery can escalate coverage and add pending audits.
Gemini requires PPQ; without it the adversarial stage runs Sol alone.

**3. Verify, edit, double-check Majors and render**

```mermaid
flowchart LR
    verify["Verify candidates<br/>+ concept evidence"] --> edit["Edit accepted findings<br/>+ concept objection"]
    edit --> major["Verify Major wording<br/>Sol 6.1 medium"]
    major --> render["Render review<br/>+ coverage limitations"]
    classDef optional stroke-dasharray: 5 4
    class edit,major optional
```

Editing runs only when findings or a concept objection survive verification.
An independently verified optional alternative goes straight to rendering.
Only findings with severity exactly `major` go through the second verifier.
This includes Major design findings. Other severities skip it. Sol checks the
final edited title and body, with independent reads of the pinned base/head,
before either the mirror review or GitHub export can publish them. It returns
verdicts only and cannot rewrite, add, merge, or downgrade findings.

Skipped audits do not make model calls. The companion service CLI requires both
OpenAI and PPQ keys. Concept research is advisory: negative, failed or incomplete
research never skips an independent, adversarial or focused code stage selected
for the review.
Reviewers do not see other discovery passes' candidates. The verifier sees the
pooled candidates, original review input, any archaeology and blind-alternative
candidates, and named inspection gaps from incomplete discovery. Code discovery
does not receive concept research output or current PR discussion.

| Stage | Default model | Reasoning effort | Per-response output token ceiling | Evidence access |
| --- | --- | --- | --- | --- |
| router | gpt-6-luna | low | 4,000 | Supplied input only |
| independent | gpt-6-luna | low | 4,000 | Full tool set |
| script | gpt-6.1-sol | high | 25,000 | Original input; code and base history, no discussion or web tools |
| adversarial | gpt-6.1-sol | high | 25,000 | Full tool set and selected domain profiles |
| adversarial_gemini | google/gemini-4-argon | high | 32,000 | Same tools and profiles, via PPQ |
| concurrency | gpt-6-luna | medium | 8,000 | Focused input and code tools |
| dead_code | gpt-6-luna | medium | 8,000 | Original input; base/head code and history, no discussion or web tools |
| public_contract | gpt-6.1-sol | low | 25,000 | Focused input and code tools |
| state, build, tests | gpt-6-luna | low | 4,000 | Focused input and code tools |
| design | gpt-6-luna | xhigh | 25,000 | Focused input, code tools and comparison-base developer notes |
| verifier, all tiers | gpt-6-luna | xhigh | 128,000 | Original input, candidates, concept candidate, code and research tools |
| major_verifier, only when Majors survive | gpt-6.1-sol | medium | 25,000 | Original input and final Major wording; code and base history tools |
| archaeologist | gpt-6-luna | low | 4,000 | Original input, base/head code tools, current discussion and research tools |
| alternatives | Same as archaeologist | low | 4,000 | Neutral problem, goal and baseline; base-only code tools |
| collator | gpt-6-luna | low | 6,000 | Accepted findings and conceptual assessment; no tools |

Both verifiers' final requests with tools disabled have a 600-second socket timeout.
Other model requests retain the 180-second timeout.

Code tools find paths, read head/base files, read diffs and search code. The full
tool set also reads base history and, when a live discussion reader is supplied,
other PR discussions. Current PR discussion is excluded from code discovery.
In frozen runs, discussion tools are available only to the archaeologist and
verifier. These stages can read current discussion and original upstream review
threads from the supplied evidence. Frozen evaluations replay only
captured research evidence and disable live discussion or web fallback. If a
research call is missing from the frozen manifest, the tool returns an
unavailable-evidence message and the model continues from code and frozen input.

Discovery allows 12 tool calls at routine tier, 24 at standard, and 48 at sensitive.
The script and dead-code specialists, both adversarial passes and both verifiers allow 48. History and discussion
sublimits are eight each per tool-enabled stage. Archaeology has twelve total
inspections. Hosted web actions count toward the stage limit and are capped at
two per response. Search fees and evidence-token headroom are reserved in the
existing OpenAI allowance; opening or finding text within a page is not a paid
search action. The early concept stage spends from the same capped review
allowance, does not estimate savings and never stops later code stages.
Script, dead_code, independent, adversarial and both verifier stages require a first inspection. At
limits, a final turn uses collected evidence and must preserve uncertainty.

The [dead-code audit](audits/dead_code.md) runs on production, test, script and
build changes, including deletions and mechanical refactors. Ordinary docs-only
changes skip it unless routing requests it. It checks both new unused code and
existing code orphaned by the PR, following removed references into unchanged
files. Literal base/head searches and caller inspection must account for
indirect uses, supported configurations, external contracts and side effects
before recommending deletion. Findings use the normal suggestion and verifier
flow; no runtime failure is required for a proven safe removal. Its 48-call
limit allows reference tracing even on routine PRs and shares the existing
spending ceiling. These settings have not been tuned against live reviews.
Custom prompt directories need `dead_code.md`, and complete custom model maps
must include `dead_code`.

OpenAI's default estimated review allowance is USD 1.00. Local PPQ use requires
an explicit separate allowance; the companion service can leave it uncapped. Routing and discovery protect OpenAI verifier headroom,
refreshed as candidates accumulate. Admission checks also protect a final model
response after inspection. Budget limits can still prevent completion, including
through competing workers' monthly spending. Failed discovery becomes a coverage
limitation; failed verification publishes no unverified findings. Failed editing
falls back to verified wording.
The Major pass uses the remaining OpenAI allowance, reserving its own final
response. It has no extra allowance or protected share during discovery. A
budget or API failure, invalid response, or missing decision withholds all
pending Majors. Explicit `drop` and `unresolved` verdicts withhold those findings;
non-Majors and concept results remain available. Medium effort and the 25,000
token limit are initial settings, not a measured accuracy guarantee.
Custom prompt directories must include `major_verifier.md`, and complete model
maps must include `major_verifier`.
The companion GitHub publisher also requires second-verifier provenance for
every Major, including saved publication retries. An unchecked batch is skipped
without posting or deleting a comment.

Public coverage separates code review, historical context, and verification.
Missing advisory history does not reduce code coverage. Expected static-review
scope, such as not running tests, is shown as a scope note rather than missing
evidence. Actual unread paths and failed stages identify their specific reason.
Failed verification has its own failed status. Candidate ID bookkeeping errors
withhold the affected decisions, preserve independently valid findings, and
leave unaccounted candidates unresolved.

Sources: [pipeline.py](ralph/pipeline.py),
[model.py](ralph/model.py), [spend.py](ralph/spend.py),
[config.py](ralph/config.py).

## How findings survive or get rejected

The successful path is short. Validation and recovery rules are listed below
so they can be read without following crossing arrows.

```mermaid
flowchart LR
    pool["Valid candidates"] --> verify["Verify claims"]
    verify --> accept["Validate findings<br/>+ assign IDs"]
    accept --> edit["Edit wording"]
    edit --> major["Double-check Major wording"]
    major --> render["Render verified feedback"]
```

| Check or outcome | What survives |
| --- | --- |
| Malformed discovery response | Discard that stage's candidates and record a limitation; keep other stages. |
| Malformed verifier envelope or decision structure | Reject the verifier response; publish no findings from it. |
| Unknown, repeated or omitted candidate IDs | Withhold affected decisions and mark affected or omitted candidates unresolved; retain independently valid findings. |
| Claim is disproved or unsupported | Drop the claim. |
| Decisive evidence is missing, or a finding fails validation | Withhold that finding and record a limitation; retain valid findings. |
| Concept objection is unsupported or invalid | Withhold the objection; retain verified code findings. |
| Editing fails, exceeds its budget, or changes the accepted ID set | Keep all accepted findings with their original verified wording and metadata. |
| Second verification rejects or cannot verify a Major | Withhold it from public findings and exports; retain the decisions in the trace. |
| Second verification fails or has invalid ID accounting | Withhold every pending Major; preserve other severities and concept results. |

The verifier may merge supplied candidates, but cannot introduce new code findings.
Every code decision must cite at least one supplied candidate ID.
It checks conceptual feedback separately from code findings. Supported concept
objections can be edited without code findings; an independently verified
optional alternative is rendered directly.
The second verifier must account for every supplied finding ID exactly once.
Its publish decisions require exact source excerpts checked against the same
immutable snapshots. The original Luna decisions remain in its stage record;
final decisions and finding attribution reflect the second pass. No editing
runs after Sol verifies the public wording.

Candidate findings are leads, not votes. Agreement between Sol, Gemini and focused
reviewers gives a claim no extra weight. A defect needs a reachable trigger and
consequence, checked against the strongest code-based reason it might be false.
Static proof can suffice; running a reproduction is not required. Defect
discovery remains independent of the concept assessment, PR discussion and
upstream review threads.

Review includes tests, CI, build tooling, contrib utilities, and non-security
defects. Security guidance draws on the
[oss-scanner Bitcoin Core threat model](https://github.com/instagibbs/oss-scanner/blob/22ee6a8367b6bd1ef308273a1cdde6aa54e7bb28/projects/bitcoin-core/threat_model.md),
adapted for static PR review. Its scanner-specific exclusions, execution
requirements, and numeric resource thresholds do not restrict Ralph's findings.

The same verifier call attaches a `security` object to each published finding:
`classification` is `potential_vulnerability`, `non_security`, or `uncertain`,
with a concise `reason`. This assessment is independent of finding kind,
review severity, and the publish decision. A supported ordinary bug survives
an unsupported security claim, and uncertain security relevance does not
discard an otherwise verified finding. Classifications are static assessments,
not proof of exploitability or safety. They appear in structured findings and
public report diagnostics; they do not add labels to review Markdown or control
routing, private triage, or publication. The existing discovery escalation flag
still controls requests for sensitive review.

Design findings need an established premise and material tradeoff. A grounded
question can survive even when the implementation is correct and the author must
supply a requirement or measurement. Uncertainty about the choice differs from
missing evidence for its premise. Suggestions need a concrete present cost and
supported alternative. Generic questions, unsupported alternatives, preferences
presented as bugs, insults and inferred motives should be rejected by the verifier.
Questions that only repeat an acknowledged tradeoff or request more measurements
without identifying a material gap should also be dropped. A change need not
improve every supported mode when the measured result in another mode is roughly
neutral; additional mode-specific policy needs a concrete benefit.
The design pass also owns missing or misleading explanations of non-obvious
rules beside changed code. A specific maintainer misunderstanding qualifies as
a maintenance cost without a runtime bug. The verifier checks the explanation's
accuracy and usefulness, rejecting obvious restatements and wording preferences.
Internal API and test-framework compatibility claims require a concrete
affected caller or established compatibility contract. Unused argument forms
and hypothetical external consumers do not establish a defect.
These are prompt requirements, not mechanical proof checks.
Discovery returns an empty findings array when no supported lead survives;
rejected possibilities and self-refutations do not belong in finding records.

Python enforces structure, candidate accounting, allowed kinds and severities,
nonempty required text, and locations in changed paths that exist on the specified
head/base side with positive integer lines. Published findings also require a
nonempty `source_evidence` array of path, starting line, head/base side, and exact
contiguous full source lines, limited to 150 lines and 12,000 UTF-8 bytes per
excerpt. A final newline is optional. Evidence may cite unchanged files; a claim
about an actual triggering call must include that caller. Python checks the excerpts
against immutable snapshot blobs and withholds mismatches. Matching source text
does not prove the claim or its consequence; the verifier must check those.
Malformed discovery rejects the entire stage result. A malformed published
finding becomes unresolved while other valid findings survive. Malformed
verifier envelopes or decision structures reject the entire verifier response.
Unknown or repeated candidate IDs withhold affected decisions while preserving
independently valid findings. Affected and omitted candidates become unresolved.

Partial coverage does not discard otherwise valid findings. An unresolved
candidate or failed stage adds a limitation. The collator cannot add, merge or
remove accepted IDs, or change their kind, severity, security assessment or
location. Python enforces those metadata constraints; preservation of meaning
is a prompt requirement.

Rendering groups findings first, with critical, major, minor, then suggestions.
Design and approach feedback follows, then at most one independently verified
optional alternative. Design kind determines its own section. A useful
alternative can be published even when the PR's overall approach is sound.
The companion adds the commit header and optional report link when publishing
a comment. Its reports expose stage replies, dispositions and attribution. Its
statistics distinguish
sole and shared accepted findings across five views: Overview, Agents, Reviews,
Outcomes, and Usage. Overview shows the leaderboard and matched adversarial
comparison; Agents links to a separate page for each stage and configured model.
Each agent page shows its execution and coverage counts, spend, and a full-width
candidate list with search, disposition filters, and pagination. Candidate rows
include PRs, verifier decisions, accepted attribution, and review report links.
Multiple candidates can merge into one accepted finding. Verifier acceptance
is not human validation or a measurement of recall. Reviews lists saved reviews;
Usage contains spend and routing diagnostics. Outcomes summarizes best-effort
automated checks of whether follow-up PR updates addressed earlier findings, keyed
by the original finding ID and head-to-head comparison. Those follow-up checks
are read from their own assessment database and spend ledger, so their budget is
reported separately from review cost. These checks are not a causal claim.
Profiles within one adversarial call get no separate causal credit.

Sources: [protocol.py](ralph/protocol.py),
[verifier prompt](audits/verifier.md), [collator prompt](audits/collator.md),
[companion report.py](../ralph-service/ralph_service/report.py),
[companion stats.py](../ralph-service/ralph_service/stats.py).

## Concept and history

When enabled, the archaeologist receives the full review input plus base/head
code tools. It runs after routing and any selected script review, before
independent discovery. It researches whether the
proposed goal and approach are worth pursuing, while code review continues no
matter what it recommends. It establishes the problem, the cost of doing
nothing, the benefit delivered by the submitted proposal, and relevant prior
attempts. It compares the proposal with the baseline and up to two credible
alternatives, identifying where each alternative came from. Promised follow-ups
do not count as delivered benefits.

Research follows explicit references before searching more broadly. Discussion
reads use bounded pages and can fetch a linked GitHub comment directly. Returned
text records retrieval time and truncation; private traces retain the evidence
sent to models. Hosted search actions and source links appear in report metadata. Historical
claims need source links. Earlier objections must be checked for later answers;
an abandoned PR does not establish that its concept was rejected. Skepticism
applies to the status quo and suggested alternatives as well as the proposal.
The brief records technical assumptions for the verifier, a conditional
recommendation, and the question or evidence most likely to change it. Its
structured proposal includes `goal`, an `assessment` of `worth_pursuing`,
`needs_motivation`, `rework_approach` or `not_worth_pursuing`, and an advisory
`proposed_review` of `continue`, `would_stop` or `undetermined` with a
`review_reason`. It does not produce code findings, assign severity or emit
ACK/NACK labels.

After a valid concept candidate, blind alternatives are enabled by default. This
uses the existing archaeologist model with base-only code tools and neutral
problem, goal and baseline inputs. Its result goes to the verifier, not to
discovery. The pipeline still runs every selected code stage.

The verifier checks the premises before accepting public conceptual feedback.
It records both the archaeologist's proposed review recommendation and its own
final recommendation. Conceptual feedback must identify a supported, actionable
objection to the PR's premise or approach. Separately, it may publish one
evidence-backed optional alternative that improves a meaningful dimension such
as simplicity, maintenance, or caller ergonomics, with its tradeoffs stated.
Favorable assessments and rejected or inconclusive alternatives stay in the
detailed trace.

Conceptual feedback has no finding ID, severity or artificial source location.
The collator can edit it even when there are no accepted code findings. It edits
finding wording and any concept objection; the optional alternative is rendered
from verifier-approved structured fields. The public comment groups findings
first, then design and concept feedback, then the optional alternative. It omits
objections already covered by accepted design findings. A question belongs in
concept feedback only if its answer settles a specific material concern.
Invalid or unverified assessments are withheld. Failed editing falls back to
verified wording.

Reports and statistics count published concept assessments separately from
findings and distinguish them from completed research. They also expose
candidate and verifier review recommendations so a human can score false
proposed stops and useful code findings that appeared after a `would_stop`
recommendation. Archaeology usage and costs remain visible, but the stage does
not compete in the finding leaderboard. These counts measure output, not
recommendation quality.
For a before/after comparison, use the same PR head and inspect whether the new
assessment recovers material history, recognises answered objections, and makes
a defensible choice between approaches. A live rerun can see newer discussion;
its evidence is not a frozen historical replay.

## Review profiles

The default profile preserves the stage settings listed above. Select the high
profile with `ralph review ... --profile high`, or set `"profile": "high"` in
an engine configuration. The CLI option overrides the configuration file.

High uses gpt-6.1-sol for independent review, concurrency, state, dead code,
design and verification. Core analysis and verification stages use at least
high reasoning effort, retaining xhigh where already configured, and at least
16,000 output tokens. Router, build, tests, archaeology, alternatives, collator,
and the PPQ reviewer retain their default settings. Tool limits are unchanged.

High multiplies the configured OpenAI review allowance by five. Its 128,000-token
verifier reserves at least $2.56 for its initial and finishing requests alone;
the larger allowance leaves room for discovery. PPQ allowances are unchanged.
The default multiplier is one. An allowance is a spend cap, not a target.

A resolved profile records every stage's model, reasoning effort, output ceiling,
and tool limits in the engine configuration snapshot and hash. Profiles keep
routine, standard and sensitive tool limits explicit. Model overrides apply after
profile selection; overriding the archaeologist model also selects that model
for alternatives. Custom audit prompt directories contain prompts only.

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
existing tool budgets. Reviews remain static; they do not execute builds or tests.

The local CLI detects commit trailers and uses available local objects without
network access. It reports unverified source PR membership as a limitation. The
companion service acquires source PR metadata and pins upstream history before
calling the engine. Neither mode requires a personal fish function or skill path.

## Author feedback on subsequent reviews

Re-reviews can carry findings from previous published reviews of the same PR.
Independent discovery still reads the new code without author replies. A bounded
feedback pass then maps explicit author replies to earlier findings and current
candidates. The verifier checks the technical claims against source code.

An author saying “addressing elsewhere” or “won't fix” does not resolve a finding.
A repeat may move into the report's previously discussed section only when the
verifier confirms the same mechanism and consequence, with evidence from both
base and head that the PR does not introduce or worsen it. Findings that remain
unresolved are retained with their original identity, severity and assessment
history, including when discovery does not find them again. Missing or invalid
reconciliation keeps them unresolved. Fixed and invalid decisions require source
evidence; author intent alone is insufficient.

This memory is restricted to one repository and PR. First reviews do not invoke
the feedback pass. The structured `review_memory` export preserves the history;
report summaries and downstream consumers must account for unresolved entries as
well as newly published findings. Frozen evaluation cases include the captured
feedback packet and its identity checks.
