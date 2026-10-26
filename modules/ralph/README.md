# ralph

This module runs `ralph-service`, a webhook receiver that posts a
first-pass review on Forgejo pull requests.

The default `services.ralph.engine = "current"` selects the existing review
pipeline. The module defaults to the `ralph-service` companion package, which
imports the core review runner. The standalone `ralph` binary now reviews local
committed changes and accepts no daemon flags. Explicit package overrides must
select `packages.<system>.ralph-service`. Ralph can load other trusted engine
modules shipped with its package;
each engine owns its review format and may provide optional findings or
actionable exports. Missing exports mean that consumer is unavailable for that
run, not that its result is empty. The detailed review behavior below describes
the current engine only.

ralph fetches the base branch and PR head into its own state directory,
reviews the PR title, description, commit messages, and diff, and gives the
model read-only tools for path discovery, numbered head and merge-base file
reads, per-file diff reads, and literal code search. Large patches are replaced
by a changed-file list so the model can read relevant diffs on demand. It never
builds, runs, or tests pull request code.

Three worker threads review different PRs concurrently by default. Set
`services.ralph.workers` (or `--workers`) to change this limit.
Reviews of the same PR run serially; newer heads wait for the active attempt to
finish. Fetches and snapshot creation share a lock in one Git object store.
Each active review pins its own base and head refs until it finishes, and tools
read immutable blobs without a checked-out worktree. Model calls run outside
the Git lock. Spending reservations remain atomic across all workers, including
the shared monthly allowance.

The model can also search and read other public issues and pull requests in the
same repository, including a small sample of ordinary comments. The current PR
is excluded from code discovery. After routing and before independent code
discovery, a separate Luna archaeologist receives the full review input, base
and head code tools, current discussion, original upstream reviews, previous
attempts and alternatives. Its concept candidate goes to the verifier with the
pooled code candidates.
These reads use no Forgejo credentials and are limited to eight calls per
tool-enabled stage.
It can inspect up to 20 lines of merge-base blame and read a related ancestor
commit's message and file diff, with eight history calls per tool-enabled stage.
Tool output is capped.

A Luna router selects relevant specialists even for sensitive paths. Code rules
set a minimum risk tier and require the applicable domain checks; they do not
select every audit just because a change is sensitive. Incomplete input or
failed routing requests the general set, excluding the script specialist. Routine changes use Luna.
Header changes follow their domain's sensitivity rules; the `.h` extension
alone does not force a full review. The router and reviewers can escalate
based on changed behavior and inspect relevant headers with their tools.
Ordinary RPC, CLI, build and CI changes have a standard floor and keep their
matching specialist audits. Authentication, untrusted input, secrets, dependency
provenance and release integrity can still trigger sensitive review. Overlapping
critical domains retain their sensitive floor, including wallet RPC code.
Documentation alone does not inherit a directory's sensitive floor.
See the [routing contracts](../../pkgs/ralph/README.md#audit-selection).
The `script` specialist is selected by the router only for substantive changes
to script execution/flags, signatures, witness/annex rules, spend-type acceptance,
or transaction validity and fee-bumping constraints. File paths and sensitivity
alone do not select it. Documentation, test-only changes, mechanical refactors
and unrelated validation/policy work skip it. Generic fallback and overview
escalation also skip it; explicit full and shadow modes request it.
It uses Sol 6.1, high reasoning, 25,000 tokens per response and 48 code/history
inspections, with no discussion or web tools. Selected script review runs before
concept research, shares the existing OpenAI allowance, and protects verifier
headroom. Partial coverage reaches the verifier as named inspection gaps and
remains visible in reports and stats. See the [specialist design](../../pkgs/ralph/README.md#script-and-transaction-policy-specialist).

Sensitive changes receive parallel independent Sol 6.1 and Gemini 4 Argon
adversarial reviews. Gemini uses PPQ at `https://api.ppq.ai/v1/responses`.
Selected consensus, wallet, and P2P profiles add the same domain instructions
to both reviews. Concurrency covers C++ lifetime, locks and shared state; state
covers persistence and recovery. Public-contract and build audits cover exposed
behavior and portability respectively. These correctness checks run before
tests and design. Reviewers can escalate when inspection reveals sensitive
behavior. Independent reviewers never receive each other's candidates. They also
never receive the archaeology output or current PR discussion.

The tests audit starts with changed production behavior and existing coverage,
even when no tests changed. It then judges assertions, fixtures and runtime.
Design owns architecture, simplicity and applicable merge-base developer notes;
there is no separate developer-notes stage. Pure test changes can take the
routine route when the router establishes that deeper checks are unnecessary.

The Luna `dead_code` audit runs on code, test, script and build changes,
including deletions and refactors. It finds new unused code and existing code
whose last use the PR removes, including definitions in unchanged files.
Ordinary docs-only changes skip it unless routing requests it. It uses medium
reasoning, 8,000 tokens per response and up to 48 base/head code and history
inspections under the existing review budget. It checks indirect uses, supported
configurations and side effects before proposing removal. Candidates pass
through the normal verifier.

The design audit also weighs practical benefit against recurring contributor
work, maintenance, confusion and reviewer attention, even when the code is
correct. It compares the claimed outcome with what the mechanism guarantees.
Grounded design questions can be published when their factual premise is
verified and the answer would settle a material tradeoff.

The archaeologist returns a source-backed concept candidate about the problem,
baseline, delivered benefit, history and credible alternatives. It can inspect
base and head code to check what the PR delivers, but it does not publish code
findings or assign severity. Its `assessment` is one of `worth_pursuing`,
`needs_motivation`, `rework_approach` or `not_worth_pursuing`; its advisory
`proposed_review` is `continue`, `would_stop` or `undetermined`, with a
`review_reason`. Negative, failed or incomplete archaeology never skips any
selected code stage. The verifier checks decisive claims and technical
assumptions before publishing supported, actionable objections to the PR's
premise or approach, including when no code findings survive. It also records
its own final review recommendation. Public "Concept and approach" feedback is
a short paragraph that omits concerns already covered by design findings.
Separately, an "Alternatives" section may contain at most one independently
verified suggestion that improves a meaningful dimension under the PR's actual
requirements, with its tradeoffs stated. It can appear even when the PR's overall
approach is sound. Favorable assessments and rejected or inconclusive alternatives
stay in the detailed trace. Unsupported assessments are withheld; editor failure
retains the verified wording. Alternatives use verifier-approved structured
fields directly.

After a valid concept candidate, blind alternatives are enabled by default. This
uses the existing archaeologist model with base-only code tools and neutral
problem, goal and baseline inputs. Its result goes to the verifier, not to
discovery, and code review still runs every selected stage.

The [core stage table](../../pkgs/ralph/README.md#stages-models-and-evidence)
lists the default models, reasoning effort, response token ceilings and evidence
access for every stage. Token ceilings cover reasoning and visible output
combined. Debug output records the settings and actual usage. OpenAI stages
share their per-review spending ceiling. The two PPQ passes share a separate
ledger and have no spending ceiling by default.

The public review presents findings first, then accepted design and concept
concerns under "Design and approach", then optional "Alternatives". Empty
sections are omitted. The Luna writing pass edits wording but cannot remove
accepted findings or change their classification. It retains its 6,000-token
output limit; an incomplete or invalid edit falls back to the verifier's wording
without truncating findings.
After editing, surviving findings with severity exactly `major` receive a
second verification by `gpt-6.1-sol` at medium effort, including Major design
findings. It checks the exact public wording using code and base-history tools,
with 48 inspections and a 25,000-token response ceiling. Only explicit approval
allows a Major into the mirror review and GitHub export. Rejected or unresolved
Majors are withheld; a failed or invalid second pass withholds all pending
Majors while preserving other severities and concept results. No model edits
the wording after this pass.

Routine discovery stages get up to 12 tool inspections, standard stages 24,
and sensitive stages 48. The adversarial passes and verifier each get
48. Archaeology gets twelve total inspections, including hosted web actions,
with at most two hosted actions per response. Search charges are included in
the OpenAI spending allowance. The concept stage spends from the same capped
review allowance as the OpenAI code stages, does not estimate savings and never
stops later code stages. With an allowance of N inspections, the model can make
up to N tool calls across at most N + 1 responses, leaving a final response
without tools. The router and writing pass have no tools. The final response and
all inspections remain subject to the spending ceiling. At the inspection limit,
further reads are refused. The model returns supported findings and names
material unanswered evidence. Debug output records the limit and which requests
were skipped.

Discovery stages return structured candidates. The verifier accounts for each
candidate as published, dropped or unresolved, and the writing pass receives
accepted findings and a separately verified conceptual assessment. Python checks
finding IDs and preserves verified
locations and severity. A bad verifier finding is withheld without discarding
other valid findings, and debug output identifies the validation error. Partial
coverage alone does not discard findings. Neither verifier may invent code
findings. Empty, unknown or repeated candidate IDs
withhold affected decisions while preserving independently valid findings;
affected and omitted candidates become unresolved. A malformed verifier envelope
or decision structure rejects the entire response. If only editing fails, the
verified wording is used.

Each verified finding also carries a security classification and reason in
structured results and report diagnostics. `potential_vulnerability`,
`non_security`, and `uncertain` are independent of review severity and whether
the finding deserves publication. This uses the existing verifier call and
does not select additional audits or change publication behavior.

The default OpenAI per-review spending ceiling is USD 1.00. It is an allowance for
complex reviews, not a target spend. Lightweight routing still skips both
adversarial passes and irrelevant specialist stages. Before each API request
ralph reserves a conservative input/output cost estimate, holding some allowance
for verification based on its actual model, input, response limit and bounded
evidence headroom.
The pipeline refreshes that protection as candidates accumulate or risk changes.
Tool-enabled requests also preserve room for a final response. When another
inspection cannot fit, the model is asked to finish from available evidence if
that final request fits. This is bounded headroom, not a guarantee of complete
verification: evidence growth and shared monthly spending can still exhaust it.
Editing uses remaining allowance and can fall back to the verifier's wording.
The second verifier also uses remaining allowance and reserves its own final
response. It has no separate allowance or protected share during discovery;
insufficient budget withholds the pending Majors. Medium effort is an initial
setting and has not been measured against a review-quality evaluation set.
Reported usage settles the reservation;
missing usage or an ambiguous transport failure retains conservative estimated
charges. These estimates depend on configured model prices and API token
accounting, so they are not an invoice guarantee. Unknown model prices prevent
requests.
The optional monthly allowance is unset by default. The ledger retains monthly
totals and includes failed requests whose charges are uncertain.

Jobs are persisted before the webhook receives a 202 response. Pending PR
updates are coalesced, stale work stops between model requests, and completed
review payloads are saved before publication. Publication retries reuse that
payload. Transient failures have bounded retries; pending claims recover when
the service restarts. ralph retains one editable comment per PR.

The public PR comment contains the marker, base and head commits, and verified
review text. When full reports are configured, it also links to the report. It
does not include a collapsed debug section, finding-attribution table, or
clipped preliminary stage replies.

Finding attribution is still recorded with the final title, discovering agents
and their models, verifier and editor. Merged candidates retain all their
source agents. Surviving Majors name the second verifier and its model; all
findings must originate in discovery. Editing never earns discovery credit. Per-stage summaries record
candidate dispositions, accepted findings found solely or jointly, concept
recommendations, cost estimates and incomplete usage. Joint credit is not
evidence that each agent was necessary, and verifier acceptance is not a human
quality label.
Adversarial records include selected profiles without attributing findings to
an individual profile within the shared call. Private traces preserve full
responses and per-request usage, including failures.

For full browser reports, set `services.ralph.reportDir` to a
separate public directory and `services.ralph.reportBaseUrl` to
the HTTP URL serving it. The module creates the directory with mode `0755`;
ralph writes readable static HTML and JSON files before publishing a link
in the review comment. Configure your web server to serve this directory
without directory browsing. Do not serve the private `stateDir` or its
`review-traces` directory.

Reports include full preliminary agent replies, coverage limitations, and
review details. Preliminary candidates remain unverified; the main review
contains verified findings. Public reports exclude private request payloads
and configuration. Each review run has a distinct URL, and publication retries
reuse it. If a report cannot be written, the review is published without a
report link. Existing comments and private traces are not backfilled.

When reports are configured, a timer also refreshes `reportDir/stats/index.html`
and its public JSON download every five minutes. The dashboard reads durable
review jobs and the review and follow-up spend ledgers without model calls or credentials. It
ranks agents and models by verifier-accepted findings, separates sole and shared
contributions, and shows execution opportunities, candidate dispositions,
coverage, routing, token usage, costs, and review details. Shared findings earn
credit for each contributing agent, so leaderboard counts overlap. Concept
assessments have separate saved/published counts and do not inflate finding
counts. Candidate and verifier `would_stop` counts are shown with downstream
findings and costs so a human can judge false proposed stops and useful findings
that appeared after a proposed stop. Archaeology costs and execution remain
visible outside the finding leaderboard.

The Usage page shows total API cost for today, this week and this month, with
daily, weekly and monthly charts covering the last 30 days, 12 weeks and 12
months. These totals include OpenAI reviews, PPQ reviews and follow-up requests.
Periods use request start times in UTC, Monday-starting weeks and calendar
months; current periods are to date. Older requests have only an accounting
month, so their costs remain in monthly totals and are identified as undated
instead of being assigned invented days. Reservations and requests with unknown
cost remain separate from recorded costs. Review-only breakdowns retain their
existing scope.

Addressed-finding checks run outside the main review service. When reports are
configured, `ralph-followup.timer` runs every five minutes with its own budget,
checkout and ledgers under `stateDir/followup/`. It stores Git data in
`stateDir/followup/checkout`, assessment rows in
`stateDir/followup/assessments.sqlite3`, and model spend in
`stateDir/followup/spend.sqlite3`. The first run seeds the local history needed
for future synchronize events; it does not automatically backfill old PR
updates. `services.ralph.followupBudgetUsd` defaults to `0.10`. Follow-up
assessment spend is shown in the addressed section, separate from main review
costs.

Distinct review jobs and paid request attempts are counted separately. Retries
increase request counts and ledger spend; they do not duplicate the final
findings. Known costs and unsettled reservations are shown separately, including
failed requests. Verifier acceptance is not human validation or a recall
measurement, and summed model time is not elapsed review time.

The page is reachable under the report URL's `stats/` subdirectory. Hosts may
also serve `reportDir/stats` at a shorter URL such as `/stats/`. To refresh it
manually without waiting for the timer:

```sh
systemctl start ralph-stats.service
```

To force a fresh review of the same head, send a signed synthetic pull request
webhook with `"ralph_force": true`. Normal Forgejo webhooks omit this field.
For a historical review, also supply `pull_request.base.sha` with the recorded
base commit. Merged PRs use the merge commit's first parent; closed, unmerged
PRs use their recorded base SHA. Only forced requests honor this field. Open
PRs omit it so the worker fetches the current target branch. Every review uses
`merge-base(base, head)..head` for its patch and file tools. Historical reviews
remain retrospective: discussion and web research are not time-filtered.
A forced review receives a new allowance. Changing prompts does not
automatically rerun previously reviewed heads.

## Review status images

The HTTP listener serves `GET /assets/<owner>/<repo>/<pr>.png` for the
configured repository. It returns a transparent 16 by 16 PNG dot:

- Grey when there is no published actionable feedback for the latest queued
  generation, including absent, pending, failed, skipped, partial or empty
  reviews without findings or a conceptual concern.
- Bitcoin orange (`#f7931a`) when the published review has verified findings
  or a conceptual concern.

Orange still applies when published feedback comes from a partial review.
Neither colour indicates approval or a pass/fail verdict.

A new queued generation resets the dot to grey until publication completes.
Existing saved results work immediately; no model calls or backfill are needed.

Forward `/assets/` to Ralph's listener alongside `/webhooks/forgejo`, preserving
the full path. This endpoint works independently of `reportDir`; the proxy
does not need access to private state. Responses use `Cache-Control: no-store`,
though external image proxies may apply their own caching.

For example, with `repository = "bitcoin/bitcoin"` and a host proxying the
listener at `ralph.example.org`:

```html
<img src="https://ralph.example.org/assets/bitcoin/bitcoin/123456.png"
     width="16" height="16" alt="Ralph review status">
```

## Minimal configuration

```nix
{
  imports = [
    inputs.will-nix.nixosModules.ralph
  ];

  services.ralph = {
    enable = true;
    origin = "https://git.example.org/owner/repo.git";
    repository = "owner/repo";
    forgejoApi = "https://git.example.org/api/v1/repos/owner/repo";
    botLogin = "ralph";

    openaiKeyFile = "/run/secrets/ralph/openai-key";
    ppqKeyFile = "/run/secrets/ralph/ppq-key";
    webhookSecretFile = "/run/secrets/ralph/webhook-secret";
    forgejoTokenFile = "/run/secrets/ralph/forgejo-token";
  };
}
```

Set the Forgejo webhook to `POST` JSON to
`https://YOUR_HOST/webhooks/forgejo`. Configure a long random secret in Forgejo
and store the same value in `webhookSecretFile`. Select custom pull request
events.

## Options

Required deployment options:

- `services.ralph.origin`: Git remote URL used for `git fetch` and
  stale-head checks.
- `services.ralph.repository`: Forgejo repository full name, for
  example `owner/repo`.
- `services.ralph.forgejoApi`: Forgejo repository API URL ending in
  `/api/v1/repos/owner/repo`.
- `services.ralph.openaiKeyFile`: file containing the OpenAI API
  key.
- `services.ralph.ppqKeyFile`: file containing the PPQ API key.
- `services.ralph.webhookSecretFile`: file containing the Forgejo
  webhook secret.
- `services.ralph.forgejoTokenFile`: file containing the Forgejo API
  token.
- `services.ralph.botLogin`: Forgejo login that owns the review
  comment.

Useful defaults:

- `services.ralph.listenAddress = "127.0.0.1"`
- `services.ralph.port = 8765`
- `services.ralph.stateDir = "/var/lib/ralph"`
- `services.ralph.commentMarker = null`, which uses
  `<!-- ralph:${repository} -->`
- `services.ralph.promptFile = null`
- `services.ralph.auditPromptDir = null`
- `services.ralph.followupPromptFile = null`
- `services.ralph.followupModel = null`

Unset main-review prompt overrides resolve inside the imported core package.
Follow-up resolves its own bundled prompt and model independently. Set
`followupPromptFile` and `followupModel` to customize follow-up; main-review
`promptFile`, `auditPromptDir` and `modelsJson` never affect it.

Set `repositoryUrl` only when the HTML URL in Forgejo webhook payloads cannot
be derived from `forgejoApi`.

## Review engine

- `services.ralph.engine = "current"`: select the current review pipeline.
  Other names load trusted Python engine modules included in the selected
  package. Loading an engine runs its code with the service's permissions, so
  only use package code you trust.
- `services.ralph.engineConfig = { }`: JSON-serializable object passed to the
  selected engine. Ralph writes this configuration to the Nix store and pins
  its resolved settings when queuing each run. Do not put secrets in it.
  The current engine accepts resolved `prompts`, or `prompt_file`,
  `audit_prompt_dir` and `models_json`, plus `profile`, `routing_mode`, `archaeology` and
  `blind_alternatives`.
  The service enables archaeology for PR context with discussion access.

The current-engine options `promptFile`, `auditPromptDir`, `modelsJson` and
`routingMode` remain convenience defaults for the current engine. Values in
`engineConfig` take precedence over those defaults. Setting any of these
current-only options explicitly with a non-current engine is rejected.
Engine-produced Markdown is the published review body. Structured data stays
private unless an engine explicitly provides a supported export.

Jobs retain their selected profile and resolved engine configuration across
retries and restarts. Each attempt records its actual engine code identity.
Saved results keep their original identity and only retry publication. Frozen
evaluation captures pin code and resolved settings too; recapture old manifests
after a schema cutover.

On service startup, Ralph checks for `jobs.sqlite3` and runs the database
conversion before starting the worker. A fresh state directory has no database
yet and skips the conversion. An already converted database is a no-op. If
conversion fails, the service does not start; fix the reported issue and restart
it. The stats and followup services require Ralph, so their timers wait for this
startup step to succeed.

For a manual conversion, stop Ralph's stats and followup timers and services,
then stop `ralph.service` and run:

```sh
ralph-service --migrate-state --state-dir /var/lib/ralph
```

The conversion creates an exclusive mode-0600 backup at
`/var/lib/ralph/jobs.sqlite3.pre-engine.bak` before converting saved results.
Historical results retain their bodies and current-engine diagnostics, but have
null code/config identity because those values were not recorded. Never-started
pending jobs without a recorded configuration bind the configured engine when
claimed. If the backup path already exists,
the conversion stops rather than overwrite it. A Nix version rollback alone
does not restore the old database format. This backup covers the jobs database
at conversion time; after new reviews run, restoring it also requires
reconciling later jobs, spend and followup records, and published outputs.

On the profile cutover, unfinished jobs with recorded model maps are converted
once to resolved `default` profiles with those models. Saved results retain
their original identity. Frozen evaluation uses schema version 5; recapture
older cases before replaying them.

## Cost and routing options

- `reviewBudgetUsd = 1.00`: base per-review spending ceiling. The `high` profile
  uses five times this OpenAI allowance. It spans retries and is not a target
  spend. The PPQ allowance and shared monthly ceiling are unchanged.
- `ppqReviewBudgetUsd = null`: optional ceiling shared by the PPQ passes, using
  `ppq-spend.sqlite3`. Each PPQ pass preserves room for its own final response.
- `monthlyBudgetUsd = null`: optional OpenAI ceiling on the month's recorded charges
  and outstanding reservations.
- `routingMode = "enabled"`: apply conservative routing.
- `routingMode = "shadow"`: record the proposed route while requesting every
  audit. This costs more and still respects the same allowance.
- `routingMode = "full"`: request every audit without calling the router.
- `modelsJson = null`: optional per-stage replacement for the model map.
  The map must include router, independent, script, adversarial, adversarial_gemini, concurrency, state,
  public_contract, tests, dead_code, design, build, archaeologist, verifier,
  major_verifier and collator.
  Prices must also be supported by ralph's ledger.

Custom main-review model maps must remove `addressed_findings`. Follow-up now
uses its own prompt and model, configured with `followupPromptFile` and
`followupModel`. Main-review audit directories no longer require
`addressed_findings.md`. Change these configurations together with the companion
package, retaining all existing state and follow-up initialization metadata.
The package split adds no database migration.

Cost history adds a request timestamp to each of the three spend databases.
The service startup hook runs the one-time migration, preserving old rows with
unknown timestamps and taking SQLite backups named `*.pre-timestamps.bak`.
For manual upgrades, run `ralph-service --state-dir /var/lib/ralph --migrate-state`
before generating stats with the new package. Missing databases are skipped.

Custom prompt directories must include `major_verifier.md`, `dead_code.md`, `consensus.md`, `wallet.md`, `p2p.md`,
`concurrency.md`, `build.md` and `archaeologist.md`. Custom model maps must
include `major_verifier`, `dead_code` and `archaeologist`; old complete maps are rejected.
Update custom prompt/model configurations together. Unsupported model names
are rejected when configuration loads. The existing verifier model override
can be used for controlled Sol comparisons while the default stays on Luna.

Enabled routing is the default to control spend. Routing rules and contracts
are covered by local tests; model quality and recall still need evaluation on
representative frozen PRs. No production-quality claim follows from those
unit tests.

The service writes a monthly spend summary to its journal after each job.
Read the ledger directly without an API key:

```sh
ralph-evaluate spend --state-dir /var/lib/ralph
```

Use the service account or another account permitted to read its private state.
The summary includes outstanding reservations in estimated_total_usd;
reserved_total_usd is the portion whose charge has not been settled.
usage_complete is false when any reported count or response is missing.

## Frozen prompt and routing experiments

The companion package installs `ralph-evaluate` for capture, replay, spend
inspection and offline summaries.
Capture performs Forgejo/Git reads but makes no model calls:

```sh
ralph-evaluate capture \
  --state-dir ./evaluation-state --output-dir ./cases \
  --origin https://git.example.org/owner/repo.git \
  --repository owner/repo \
  --forgejo-api https://git.example.org/api/v1/repos/owner/repo \
  --forgejo-token-file /run/secrets/forgejo-token \
  123
```

Capture retains complete Git objects in a dedicated evaluation checkout. The
initial download can be large. Keep that checkout with the manifests.
Capture stores bounded current PR discussion by default. Use
`--research-requests-json` to add exact prior discussion calls to the manifest,
and `--research-cutoff` to cap visible discussion at an ISO timestamp. The
requests file is a JSON list of objects with `name` and `arguments`; supported
names are `search_discussions`, `read_discussion`,
`read_current_pr_discussion` and `read_github_discussion`.
Replay reads frozen title/body, commit IDs, Git objects, prompt/model
configuration and captured research evidence. It disables live discussion, live
web fallback and lazy Git downloads. If archaeology or verification asks for a
research call that was not captured, the tool reports unavailable frozen
evidence and the model continues from code and the frozen input:

```sh
ralph-evaluate run \
  --state-dir ./evaluation-state --output-dir ./results \
  --openai-key-file /run/secrets/openai-key \
  --ppq-key-file /run/secrets/ppq-key \
  --review-budget-usd 1.00 \
  ./cases/case-123-*.json
```

Run supports `--prompt-file`, `--audit-prompt-dir`, `--models-json`,
`--routing-mode` and `--no-blind-alternatives` for ablation. Blind alternatives
are on by default after a valid concept candidate. The blind run keeps the
existing archaeologist model, gives it only base-side code tools and neutral
problem, goal and baseline inputs, and sends the result to the final verifier
rather than discovery. Each run gets a distinct private JSON artifact with
effective configuration identity, raw stage results, research inventory, usage,
final comment and any failure. Optional `--labels-json` maps case IDs to
expected findings; these labels are saved for comparison and never sent to
reviewers.

Compare useful findings and missed known findings alongside cost, incomplete
coverage and wall time. An empty review is not proof of a good route. Shadow
routing is useful for a bounded comparison before changing sensitive path
rules or removing a specialist.

See [the evaluation guide](evaluation.md) for offline summaries and a human
scorecard for comparing coverage and cost. Summaries use recorded attribution;
they do not automatically decide whether a finding is correct or useful.

The service and evaluation run command require `--ppq-key-file`. Compare
`adversarial` and `adversarial_gemini` in debug stage metrics for sole/shared
accepted findings and published, dropped or unresolved candidates. OpenAI has a
USD 1.00 default ceiling; PPQ passes have no ceiling by default. Incomplete
passes are not evidence of model quality.
PPQ rates use the model catalog input, output and cache prices. Unknown
cache-write usage uses the higher input rate as a conservative estimate.
See [PPQ integration guide](https://ppq.ai/llms.txt).

### GitHub major findings

Set `services.ralph.githubPostMajorFindings = true` to enable GitHub comments.
It defaults to `false`. Configure `githubRepository` with the upstream
`owner/repo` and `githubTokenFile` with a runtime secret file. The upstream and
mirror must use matching PR numbers. Public reports must also be enabled through
`reportDir` and `reportBaseUrl`.

Set `githubPostMajorFindings = false` to stop GitHub posting while retaining
the repository and token configuration. Forgejo reviews and the second Sol
verification of Major findings still run. Existing GitHub comments remain.

Ralph keeps one GitHub comment per PR, containing only findings whose severity
is exactly `major`, using their final edited wording, plus a full-report link.
A later review updates that comment or deletes it when no major findings remain.
Skipped reviews and engines without a findings export leave it alone. Updates
and deletion require an open upstream PR at the reviewed head. The Forgejo
comment is unchanged. Publication failures use the existing saved-result retry
queue without repeating the model review.
Every exported Major must record `verified_by = "major_verifier"`. If any lacks
that approval, the GitHub operation returns `unverified` before making requests
and leaves any existing comment alone. This also blocks unchecked Majors from
saved publication retries created before the second verifier was introduced.
Historical mirror reports remain unchanged; rerun their review to apply the new
verification policy.

Use a token for Ralph's GitHub account that can read the upstream PR and create,
edit and delete its own issue comments. See [GitHub's token permission notes](https://docs.github.com/en/rest/issues/comments).
For an account outside the upstream organization, use a classic personal access
token with `public_repo` scope. Fine-grained tokens owned by the bot account
provide only read access to other owners' public repositories. See
[GitHub's token limitations](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#fine-grained-personal-access-tokens-limitations).
Never put the token itself in Nix configuration or the Nix store.

### Telegram publishing and monitoring

Set `services.ralph.telegram.enable = true` to publish completed reviews and
monitor Ralph in a Telegram channel. It defaults to `false`. Public reports
must also be configured through `reportDir` and `reportBaseUrl`.

```nix
services.ralph.telegram = {
  enable = true;
  botTokenFile = "/run/secrets/ralph/telegram-token";
  chatId = "@ralph_reviews";
};
```

Create a bot with Telegram's `@BotFather` and add it to the channel as an
administrator with permission to post messages. Store its token in the runtime
secret file, readable by the Ralph service user. Never put the token itself in
Nix configuration or the Nix store. For a public channel, `chatId` is its
`@username`. For a private channel, use its numeric chat ID as a string.
These are the only Telegram credentials Ralph needs.
The monitor only accepts commands in this channel. It ignores direct messages.
All channel administrators may issue commands. Regular subscribers cannot
post. Telegram can hide individual user IDs on channel posts, so anonymous
posts rely on the channel's posting permissions. Identified senders must be
current administrators, and the monitor verifies that the bot can post before
queuing a reply. Displayed author signatures are not used for authorization.

Post `/status` to see the current states of `ralph.service`,
`ralph-followup.service`, `ralph-stats.service` and `ralph-telegram.service`,
plus queue counts, review results and estimated API costs. `/help` lists the
commands. Post `/high 123` to request a fresh high-profile review of PR #123 in
the configured repository, even if its current head was already reviewed.
The reviewer checks that the PR is open before queuing it. These commands
cannot restart services.

The `default` profile preserves the existing review settings. `high` increases
model capability and reasoning for core analysis and verification while keeping
routine stages and the external adversarial models unchanged. Stage settings
include model, reasoning effort, response token ceiling and tool-call limits.
See the [core profile documentation](../../pkgs/ralph/README.md) for details.
Normal webhook reviews use `default` unless `engineConfig.profile` selects
`high`. Profile selection is explicit; routing does not automatically escalate.

`ralph-telegram.service` runs separately from `ralph.service`, so it can answer
when the reviewer is stopped or failing. It starts on boot with its own
restart policy and needs no additional token. The review daemon no longer
receives Telegram credentials. The monitor polls Telegram, so no webhook or
inbound firewall rule is needed.

Each post includes the PR title, PR link and full report link. Ralph includes
the full review when the complete post fits Telegram's 4,096-character limit,
counted in UTF-16 code units. Longer reviews get the title and links only.
Reviews with an exported finding whose severity is exactly `major` receive a
`🚨 Major finding` marker in either form.

The monitor checks for completed reviews and terminal job failures every 30
seconds. Each check groups new terminal failures into one alert. The daily
digest runs at 21:00 UTC and reports the preceding 24 hours' observed review
completions and failures, current queue depth, and estimated spend by request
date across the OpenAI, PPQ and follow-up ledgers. Missing ledgers and unknown
costs are identified. The reporting window runs from 21:00 to 21:00 UTC.
Counts use observation time because the job queue does
not record completion timestamps.

On first enable, existing completed and failed jobs form a baseline and are
not posted. The first digest arrives at the next 21:00 UTC check. After
downtime, the monitor counts newly observed events in the window it sees them
and sends only the latest completed window's digest.

Notifications and review requests live in `stateDir/telegram.sqlite3` and retry
independently of reviews. The monitor saves each `/high` request with its
Telegram update offset; the reviewer consumes it and deduplicates it in the job
database. Requests survive reviewer downtime without creating duplicate runs
after a crash. An ordinary webhook cannot downgrade a pending high review.
Preserve this database with Ralph's other state. Command replies
are sent as soon as the summary is ready, ahead of queued background posts.
Background delivery sends one message per check. All messages respect
Telegram's retry delay. A crash after Telegram
accepts a message but before Ralph records delivery can cause a duplicate.
The monitor keeps the existing database at this path; no migration is needed.
It runs without an extra systemd timer.
