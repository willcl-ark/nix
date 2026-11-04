# ralph

This module runs `ralph-service`, a webhook receiver that posts a
first-pass review on Forgejo pull requests.

Configure repositories under `services.ralph.repositories.<id>`. One coordinator,
worker pool, queue and Telegram consumer serve all repositories. IDs such as
`core` and `gui` are persistent identities, so do not rename or reuse them for a
different source. Jobs, cancellation, publication holds, reports and commands
are scoped by repository and PR number.

Global options configure the account, state/report directories, listener,
provider and worker pool. Each repository selects its source, credentials,
review strategy, concurrency allowance and optional GitHub publication policy.
GitHub publication is disabled by default. Both repositories ingest Forgejo
webhooks; native GitHub ingestion is not required.

The previous `instances` configuration is removed. Move shared settings to
`services.ralph` and repository settings to `repositories.core`. Preserve the
existing Core state directory, report directory, public URLs and comment marker.
Stop all state writers and take a verified backup before the explicit database
migration. The service refuses an unmigrated database; startup does not migrate
it automatically. Historical Core report URLs remain valid.

A GUI repository can set `strategy = "gui"` and
`submodules.bitcoin = "https://github.com/bitcoin/bitcoin"`. Only explicitly
configured submodules are prepared, at the exact commits pinned by the GUI base
and head. Mirror synchronization needs no submodule commands. Set its
`writeVerifierEnvironment` to `packages.<system>.ralph-gui-verifier-env` for Qt
experiments. Other repositories have no submodules by default.

The default `services.ralph.repositories.core.engine = "current"` selects the existing review
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
reads, per-file and PR-commit diff reads, and literal code search. Large patches
include a complete file inventory with change sizes, commit-to-path mapping,
and excerpts shared across files. Deferred reads have explicit continuation
lines. Near the end, a write-verifier can build and test isolated copies to
investigate supported Major findings. The final judge decides publication and
writes approved wording from source, rebuttal, and optional experiment evidence.
A failed build or impractical experiment does not invalidate a static proof.

The flake packages include a build environment taken from the pinned
[Bix default devShell](https://github.com/bitcoin-dev-tools/bix). Dependencies
are realized when building the package, with no Nix daemon or network needed
inside an experiment. The environment includes Bix's compiler, libraries,
Python test dependencies and shell hooks. Bix follows this flake's nixpkgs.
Override `services.ralph.writeVerifierEnvironment` with a package whose main
program sets up the environment and executes its command arguments when
reviewing projects with different build dependencies.

Experiments use Bubblewrap with private mount, user, PID, network, IPC and UTS
namespaces. The review service allows these namespaces; hosts must allow
unprivileged user namespaces. A host that blocks them records experiments as
inconclusive. The experiment sees the Nix store read-only, its source copy,
temporary files and private loopback, without Ralph's secrets or state.
The service delegates CPU, memory and process controllers to the runner.
Each experiment has an 8 GiB total memory limit, no swap, at most 128 processes
and a CPU quota of two cores. The review controller runs in a separate subgroup
so an experiment exceeding its allowance cannot kill the controller through
that limit. The runner also bounds temporary storage and serializes experiments.
Without writable cgroup delegation, experiments are unavailable and findings
continue to the final source-based check.

Three worker threads review different PRs concurrently by default. Set
`services.ralph.workers` (or `--workers`) to change this limit.
Reviews of the same PR run serially; newer heads wait for the active attempt to
finish. Fetches and snapshot creation share a lock in one Git object store.
Each active review pins its own base and head refs until it finishes, and tools
read immutable blobs without a checked-out worktree. Model calls run outside
the Git lock. Each model request has its own durable usage record.

The model can also search and read other public issues and pull requests in the
same repository, including a small sample of ordinary comments. The current PR
is excluded from code discovery. After routing, alongside code discovery,
a separate Luna archaeologist receives the full review input, base
and head code tools, current discussion, original upstream reviews, previous
attempts and alternatives. Its concept candidate goes to the verifier with the
pooled code candidates.
These reads use no Forgejo credentials and are limited to eight calls per
tool-enabled stage.
It can inspect up to 20 lines of merge-base blame and read a related ancestor
commit's message and file diff, with eight history calls per tool-enabled stage.
Tool output is capped.

A Sol 6.1 router uses high reasoning and up to eight read-only code inspections
to classify changed behavior and select relevant specialists. Paths require
matching specialist audits, but never force a tier or adversarial profile.
Comments in critical headers can be routine, and demonstrably mechanical wallet
changes can be standard. Sensitive decisions identify a changed boundary or
invariant; lower-tier critical refactors need evidence of preserved behavior.
Incomplete input, failed routing or unresolved material context requests the
general sensitive set, excluding the script specialist. Reviewers can still
escalate when their inspection reveals sensitive behavior. Consensus-domain
routing and conservative fallback promote the remaining review to high settings,
including a stronger tests reviewer. Explicit stage overrides remain effective.
See the [routing contracts](../../pkgs/ralph/README.md#audit-selection).
The `script` specialist is selected by the router only for substantive changes
to script execution/flags, signatures, witness/annex rules, spend-type acceptance,
or transaction validity and fee-bumping constraints. File paths and sensitivity
alone do not select it. Documentation, test-only changes, mechanical refactors
and unrelated validation/policy work skip it. Generic fallback and overview
escalation also skip it; explicit full and shadow modes request it.
It uses Sol 6.1, high reasoning, 25,000 tokens per response and 48 code/history
inspections, with no discussion or web tools. Selected script review runs in the
discovery pool. Partial coverage reaches the verifier as named inspection
gaps and remains visible in reports and stats. See the [specialist design](../../pkgs/ralph/README.md#script-and-transaction-policy-specialist).

Sensitive changes receive parallel independent GPT-6 Astra and Claude Opus 5.5
adversarial reviews. Claude uses `claude-opus-5-5` at `xhigh` through the same
configured OpenAI-compatible proxy, with a 64,000-token response allowance.
Selected consensus, wallet, and P2P profiles add the same domain instructions
to both reviews. Concurrency covers C++ lifetime, locks and shared state; state
covers persistence and recovery. Public-contract and build audits cover exposed
behavior and portability respectively. Discovery agents run concurrently in
a bounded pool. Reviewers can escalate when inspection reveals sensitive
behavior, which adds audits to the running pool. Independent reviewers never receive each other's candidates. They also
never receive the archaeology output or current PR discussion.

The tests audit starts with changed production behavior and existing coverage,
even when no tests changed. It groups required paths by behavior and returns
`test_coverage` with paths, status, exact source evidence, a distinguishing
regression, and a reason. Checked means assertions and implementation were
inspected, including a confirmed missing assertion. Deferred means unfinished
inspection. Not-applicable needs a concrete explanation. A production-only
review cannot establish test coverage.

When required paths remain omitted or deferred, one bounded `tests_followup`
starts as soon as tests completes and reuses its prompt and resolved settings
to inspect those remaining groups while other audits may still run.
Prior assessments and candidates prevent repeating the whole review. Checked
assignments from both passes are combined; original model replies and limitations
remain in the trace. Unfinished work remains visible after that pass; receiving
an excerpt does not complete a coverage assignment.
Design owns architecture, simplicity and applicable merge-base developer notes;
there is no separate developer-notes stage. Pure test changes can take the
routine route when the router establishes that deeper checks are unnecessary.

The Luna `dead_code` audit runs on code, test, script and build changes,
including deletions and refactors. It finds new unused code and existing code
whose last use the PR removes, including definitions in unchanged files.
Ordinary docs-only changes skip it unless routing requests it. It uses medium
reasoning, 8,000 tokens per response and up to 48 base/head code and history
inspections under the configured tool limit. It checks indirect uses, supported
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
stay in the detailed trace. Unsupported assessments are withheld. Eligible
concept objections go to the final judge for publication and final wording;
there is no fallback wording on judge failure. Alternatives use
verifier-approved structured fields directly.

After a valid concept candidate, blind alternatives are enabled by default. This
uses the existing archaeologist model with base-only code tools and neutral
problem, goal and baseline inputs. Its result goes to the verifier, not to
discovery, and code review still runs every selected stage.

The [core stage table](../../pkgs/ralph/README.md#stages-models-and-evidence)
lists the default models, reasoning effort, response token ceilings and evidence
access for every stage. Token ceilings cover reasoning and visible output
combined. Debug output records the settings and actual usage. All review stages,
including Claude, record request attempts and reported usage.

The public review presents findings first, then accepted design and concept
concerns under "Design and approach", then optional "Alternatives". Empty
sections are omitted.

After discovery and any test follow-up, the challenger attempts to refute every
supplied candidate. It returns `refuted`, `not_refuted`, or `unresolved`, with
exact source evidence for refutations. It cannot add candidates, suppress them,
or decide publication. Every original candidate reaches the verifier with its
challenge evidence.

The verifier assesses code claims as `supported`, `refuted`, or `unresolved`.
Supported findings become eligible for the judge, with validated source
excerpts, severity, security assessment, and location. Optional write-verifier
experiments investigate eligible findings whose severity is exactly `major`.
The judge uses Sol 6.1 with high reasoning, 25,000 output tokens, and 72 source
inspections to decide publication and write the final wording. High settings
raise judge reasoning to xhigh.

The judge is skipped when no supported finding or eligible concept objection
remains. Otherwise it accounts for every eligible ID and returns `publish`, `drop`, or
`unresolved`. Each publication requires exact source evidence. It cannot add or
merge findings, restore ineligible candidates, promote severity, or change kind,
security assessment, or location. No model edits its approved wording afterward.
A failed or invalid judge response withholds pending findings and concept
feedback. Earlier verifier wording is never used as fallback.

Routine discovery stages get up to 12 tool inspections, standard stages 24,
and sensitive stages 48. The adversarial passes get 72 under default settings
and 128 under high; the verifier and judge each get 72. The test follow-up reuses
the tests allowance once. Archaeology gets twelve total inspections, including
hosted web actions. The configured two hosted actions per response is advisory;
a provider may return more. The concept stage never stops selected code stages.
With N inspections, the model can use at most N tool calls across N + 1 responses,
leaving a final response without tools. At the inspection limit, further reads
are refused. Supported findings remain eligible and material unanswered evidence
remains visible. Debug output records the settings, reads, and limit hits.

Python validates candidate accounting, required text, locations, and exact
source excerpts. Unknown, repeated, or omitted candidate IDs withhold affected
verifier decisions while retaining independently valid findings. Malformed
verifier envelopes reject the response. Partial coverage alone does not discard
supported findings, but neither challenge nor verification can invent code
findings. The final judge must independently approve eligible findings before
publication.

Each verified finding also carries a security classification and reason in
structured results and report diagnostics. `potential_vulnerability`,
`non_security`, and `uncertain` are independent of review severity and whether
the finding deserves publication. This uses the existing verifier call and
does not select additional audits or change publication behavior.

Ralph records request attempts and provider-reported input, output, reasoning
and cached tokens when available. Missing usage stays unknown, including after
transport failures. It does not calculate prices, reserve funds or block work
against per-review or monthly monetary allowances. Tool limits, request
timeouts, worker concurrency and upstream subscription limits still apply.
Failed verification withholds unsupported findings. Failed final judging
withholds pending findings, with no wording fallback.

Jobs are persisted before the webhook receives a 202 response. Pending PR
updates are coalesced, stale work stops between model requests, and completed
review payloads are saved before publication. Publication retries reuse that
payload. Transient failures have bounded retries; pending claims recover when
the service restarts. ralph retains one editable comment per PR.

On SIGTERM or SIGINT, Ralph stops claiming jobs and lets active reviews finish,
including saving and publishing their results. Webhooks remain available during
the drain and queue work for the next process. Deployment therefore waits for
the active reviews, even if new work keeps arriving. There is a brief webhook
outage while the process restarts.

The NixOS service sends the shutdown signal only to Ralph's main process so
review experiments can finish too. It waits without a stop timeout; a stuck
review can hold up deployment and requires an operator to intervene. Forced
termination still uses the persisted-job recovery described above.

The public PR comment contains the marker, base and head commits, and verified
review text. When full reports are configured, it also links to the report. It
does not include a collapsed debug section, finding-attribution table, or
clipped preliminary stage replies.

Finding attribution is still recorded with the final title, discovering agents
and their models, source verifier and final judge. Merged candidates retain all
their source agents. Approved findings name the judge and its model; all
findings must originate in discovery. Challenge, verification and wording do
not earn discovery credit. Per-stage summaries record
candidate dispositions, accepted findings found solely or jointly, concept
recommendations, reported tokens and incomplete usage. Joint credit is not
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
reuse the saved report without changing its contents. If a report cannot be
written, the review is published without a report link.

New reports include **Challenger outcomes**, joining each original candidate to
the validated challenge, source verification and final publication decision.
Expand the cells for reasons and source evidence. The stats **Agents** page
summarizes challenger outcomes by model; open its agent page to search candidates
or filter for **Refuted, then approved** and other outcomes. Counts refer to
candidates, including merged candidates separately, and measure agent agreement
rather than correctness or confirmed delivery. Missing decisions stay unassessed.

Statistics use retained normalized challenge records, including earlier runs of
the refutation pipeline. Historical discovery-mode challengers are excluded from
these counts. Existing report snapshots remain unchanged; their evidence links
open the original report, where challenger replies remain under Agent Outputs.

Reports, histories and search share the stats page's green palette and light/dark
toggle. Theme assets are small local CSS and JavaScript files with no external
libraries, fonts or network requests.

Each PR has a history page at `reportBaseUrl/<PR>/`, linked as **Previous
reports** from new review comments and full reports. It lists every available
public report, including reruns of the same commit. Add `?commit=<SHA>` to
filter by a full commit hash or prefix. The archive root at `reportBaseUrl/`
provides a search form for PR numbers and commit hashes or prefixes. It stays
empty until a search is submitted. Reports appear newest first, with 🟠 beside
the most recent dated report. Undated backfills appear last and never receive
a most-recent badge. The stats timer refreshes its public
`history.json` catalog alongside the PR history pages.

History pages load the external `history.js` script. Their content security
policy must allow `script-src 'self'`, `connect-src 'self'` for the search
catalog, and same-origin form submissions. Keep the stricter report policy on
individual report HTML files. History pages are generated static HTML;
configure the web server to serve directory indexes and redirect `<PR>` to
`<PR>/`. The stats timer updates history from existing public HTML/JSON pairs,
so reports already on disk appear automatically. Older reports without a
recorded completion time use their file modification time. Saved reports may
include reviews that were never posted. Private traces are never served or
indexed directly.

To recover missing reports from completed saved jobs, run the installed service
Python environment with `python -m ralph_service.backfill --state-dir DIR
--report-dir DIR`. This previews recoverable reports; add `--apply` to write
them and refresh history. It makes no model calls or comment updates. Existing
reports are preserved, missing historical dates are shown as unknown, and
unrecoverable jobs are reported.

When reports are configured, a timer also refreshes `reportDir/stats/index.html`
and its public JSON download every five minutes. The dashboard reads durable
review jobs and the review and follow-up usage ledgers without model calls or credentials. It
ranks agents and models by verifier-accepted findings, separates sole and shared
contributions, and shows execution opportunities, candidate dispositions,
coverage, routing, token usage, and review details. Shared findings earn
credit for each contributing agent, so leaderboard counts overlap. Concept
assessments have separate saved/published counts and do not inflate finding
counts. Candidate and verifier `would_stop` counts are shown with downstream
findings and usage so a human can judge false proposed stops and useful findings
that appeared after a proposed stop. Archaeology usage and execution remain
visible outside the finding leaderboard.

The Limits page at `stats/limits/` shows recorded limits by agent, model,
review profile and cap. It separates usage reaching a cap from a limit
stopping work, with links to affected reviews. Output-token usage is the largest
single response, matching the per-response limit. Tool, discussion and history
limits record refused work or forced final answers. Web-search usage can reach
its cap without a provider reporting that more searches were wanted, so those
rows do not claim confirmed blocking.

Limits data comes from saved stage records. Older runs without telemetry are
counted as uninstrumented. Use the page to choose limits for controlled
same-head comparisons; reaching a limit alone does not prove the review would
improve with more allowance.

The Usage page shows recorded attempts and provider-reported token counts.
Unknown usage remains explicit. These totals describe Ralph's own activity;
subscription quotas also include other tools using the same accounts. New
attempts are recorded in `usage.sqlite3` and `followup/usage.sqlite3`. Old
spend databases are left untouched and are not read or converted.

To add Codex and Claude subscription quotas from CLIProxyAPI v8:

```nix
services.ralph.subscriptionUsage = {
  enable = true;
  managementUrl = "http://127.0.0.1:8317";
  managementKeyFile = "/run/secrets/cli-proxy-management-key";
};
```

Set a separate nonempty `management.secret-key` in CLIProxyAPI. Inference client
keys cannot authenticate management requests. Keep management bound to loopback
with remote management disabled. The bundled `management.html` UI is optional;
quota polling works with the control panel disabled. No separate frontend is
needed. The stats service receives the management key through a systemd
credential; the review daemon and Telegram monitor do not receive it.

Stats polls upstream usage through the management API every five minutes,
without running inference or draining the proxy's temporary usage queue. Public
snapshots contain provider, plan when available, separate quota windows, reset
times and optional credit balances. Account identities, OAuth tokens, credential
paths and management keys are excluded. Failures are shown as unavailable, never
as unused capacity. Snapshot time is visible. Provider credit balances are not
converted into dollars, and subscription fees are not supplied by these endpoints.

Addressed-finding checks run outside the main review service. When reports are
configured, `ralph-followup.timer` runs every five minutes with its own checkout
and records under `stateDir/followup/`. It stores Git data in
`stateDir/followup/checkout`, assessment rows in
`stateDir/followup/assessments.sqlite3`, and request attempts in
`stateDir/followup/usage.sqlite3`. The first run seeds the local history needed
for future synchronize events; it does not automatically backfill old PR
updates. Follow-up request and token usage is shown separately.

Distinct review jobs and request attempts are counted separately. Retries
increase request counts; they do not duplicate final findings. Verifier
acceptance is not human validation or a recall measurement, and summed model
time is not elapsed review time.

The page is reachable under the report URL's `stats/` subdirectory. Hosts may
also serve `reportDir/stats` at a shorter URL such as `/stats/`. To refresh it
manually without waiting for the timer:

```sh
systemctl start ralph-stats.service
```

Stats updates are incremental by default. A private cache in `stateDir/stats`
holds compact review summaries and source checkpoints. Database triggers record
inserts, updates and deletions transactionally, so retries and completed requests
invalidate their earlier summaries. Stores without change tracking are read in
full until their writer initializes tracking. Aggregate calculations and live
subscription usage still refresh each run; the cache never substitutes for the
authoritative review and usage databases.

To rebuild after a backfill or manual restore, run the same `ralph-evaluate stats`
command and arguments as the service with `--full` added, as the service user.
This rebuilds derived summaries and history without making model calls. Missing
caches and changes to the cache calculation version also cause a full rebuild.
Developers must bump `stats_cache.VERSION` when changing cached extraction or
interpretation. Source replacement and sequence regression invalidate checkpoints,
but an in-place database restore that preserves identity and does not regress the
sequence requires `--full`.

History checks report file metadata before parsing changed reports. Use `--full`
after edits that preserve file size and modification time. Unchanged generated
pages are not replaced; a small freshness document supplies their current update
time. The full JSON export remains available and is refreshed each run.
The stats content security policy must allow `connect-src 'self'` for the
freshness request, in addition to the existing same-origin script permission.

Automatic events schedule a new review only when the authoritative PR head SHA
changes. Engine upgrades, metadata edits and base-branch changes do not rerun an
unchanged head or replace its pending, running or retrying job.

Failures fetching the required PR refs retry the existing job up to three total
attempts, with 30-second and 60-second delays. Each attempt rechecks the PR head
before collection. These retries precede model execution; sanitized Git errors
are retained in the job and warning logs. Other Git failures are not automatically
treated as retryable.

To force a fresh review of the same head, send a signed synthetic pull request
webhook with `"ralph_force": true`. Normal Forgejo webhooks omit this field.
For a historical review, also supply `pull_request.base.sha` with the recorded
base commit. Merged PRs use the merge commit's first parent; closed, unmerged
PRs use their recorded base SHA. Only forced requests honor this field. Open
PRs omit it so the worker fetches the current target branch. Every review uses
`merge-base(base, head)..head` for its patch and file tools. Historical reviews
remain retrospective: discussion and web research are not time-filtered.
Changing prompts does not automatically rerun previously reviewed heads.

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
  imports = [ inputs.will-nix.nixosModules.ralph ];
  services.ralph = {
    enable = true;
    openaiApiBase = "http://127.0.0.1:8317/v1";
    openaiKeyFile = "/run/secrets/ralph/proxy-key";
    repositories.core = {
      origin = "https://git.example.org/owner/repo.git";
      repository = "owner/repo";
      forgejoApi = "https://git.example.org/api/v1/repos/owner/repo";
      botLogin = "ralph";
      webhookSecretFile = "/run/secrets/ralph/webhook-secret";
      forgejoTokenFile = "/run/secrets/ralph/forgejo-token";
      workers = 3;
    };
  };
}
```

Set the Forgejo webhook to `POST` JSON to
`https://YOUR_HOST/webhooks/forgejo`. Configure a long random secret in Forgejo
and store the same value in `webhookSecretFile`. Select custom pull request
events.

## Subscription inference through CLIProxyAPI

Import `will-nix.nixosModules.cli-proxy-api` alongside the Ralph module and
provision the proxy's runtime configuration and Codex and Claude logins. See
the [CLIProxyAPI module](../cli-proxy-api/README.md). Then configure Ralph:

```nix
services.cli-proxy-api = {
  enable = true;
  configFile = "/run/secrets/cli-proxy-api.yaml";
};
services.ralph = {
  openaiApiBase = "http://127.0.0.1:8317/v1";
  openaiKeyFile = "/run/secrets/ralph/proxy-key";
};
```

The key file contains the proxy's client access key, not the subscription OAuth
token. All review stages, including Claude adversarial review, and follow-up
assessments use the proxy and the same key. Ralph retains its prompts and tool
execution. A failed proxy request does not fall back to the paid OpenAI API.
The proxy converts Claude's strict JSON schema to prompt guidance; Ralph still
parses and validates the response before accepting it.

Stop new review intake and let active work finish before changing the endpoint
and key together. Queued jobs and frozen evaluations retain their recorded
engine configuration; requeue or recapture them for the new endpoint. Verify
the proxy's authenticated model list contains the configured Codex model IDs
and `claude-opus-5-5`, then run one local review with the proxy URL in `--engine-config` and inspect
its result before resuming the service queue. The local command does not publish
review comments.

CLIProxyAPI strips Codex's `max_output_tokens`, so the requested output cap
is not enforced on that path. Subscription limits are enforced upstream.
Ralph records returned usage without estimating charges or remaining quota.

## Options

Required deployment options:

- `services.ralph.repositories.core.origin`: Git remote URL used for `git fetch` and
  stale-head checks.
- `services.ralph.repositories.core.repository`: Forgejo repository full name, for
  example `owner/repo`.
- `services.ralph.repositories.core.forgejoApi`: Forgejo repository API URL ending in
  `/api/v1/repos/owner/repo`.
- `services.ralph.openaiKeyFile`: file containing the OpenAI API
  key or the configured proxy's client access key.
- `services.ralph.repositories.core.webhookSecretFile`: file containing the Forgejo
  webhook secret.
- `services.ralph.repositories.core.forgejoTokenFile`: file containing the Forgejo API
  token.
- `services.ralph.repositories.core.botLogin`: Forgejo login that owns the review
  comment.

Useful defaults:

- `services.ralph.listenAddress = "127.0.0.1"`
- `services.ralph.port = 8765`
- `services.ralph.stateDir = "/var/lib/ralph"`
- `services.ralph.repositories.core.commentMarker = null`, which uses
  `<!-- ralph:${repository} -->`
- `services.ralph.repositories.core.agentsDir = null`
- `services.ralph.followupPromptFile = null`
- `services.ralph.followupModel = null`

Main-review agent settings and prompts resolve inside the imported core package.
Set `agentsDir` to use a complete alternate agents directory. Each agent has a
`config.toml` and Markdown prompt under [pkgs/ralph/agents](../../pkgs/ralph/agents/).
Edit [pkgs/ralph/final_comment](../../pkgs/ralph/final_comment/) to change the
review comment templates. Run `python3 pkgs/ralph/review.py check-agents` from
the repository root to validate contributor edits without credentials.

Follow-up resolves its own bundled prompt and model independently. Set
`followupPromptFile` and `followupModel` to customize follow-up.

Set `repositoryUrl` only when the HTML URL in Forgejo webhook payloads cannot
be derived from `forgejoApi`.

## Review engine

- `services.ralph.repositories.core.engine = "current"`: select the current review pipeline.
  Other names load trusted Python engine modules included in the selected
  package. Loading an engine runs its code with the service's permissions, so
  only use package code you trust.
- `services.ralph.repositories.core.engineConfig = { }`: JSON-serializable object passed to the
  selected engine. Ralph writes this configuration to the Nix store and pins
  its resolved settings when queuing each run. Do not put secrets in it.
  The current engine accepts resolved `prompts`, or `agents_dir`, plus
  `profile`, `routing_mode`, `archaeology`, `blind_alternatives`,
  `openai_api_base` and `stages`.
  The service enables archaeology for PR context with discussion access.

The current-engine options `agentsDir` and `routingMode` provide convenience
defaults for the current engine. Values in `engineConfig` take precedence over
those defaults. These convenience defaults apply only to the current engine.
Engine-produced Markdown is the published review body. Structured data stays
private unless an engine explicitly provides a supported export.

`services.ralph.openaiApiBase` defaults to `https://api.openai.com/v1`.
It sets the current engine's OpenAI-compatible Responses endpoint and the
follow-up endpoint. An explicit `engineConfig.openai_api_base` takes precedence
for both. Credentials belong only in `openaiKeyFile`, never in the URL.

Use `engineConfig.stages` to override individual settings on any existing
review stage. Overrides apply after the agent's settings for the selected
`default` or `high` profile. Unspecified settings keep their resolved defaults.
The verifier uses Claude Opus 5.5 with extra-high reasoning in both profiles.
For example, to reduce verification reasoning to high:

```nix
services.ralph.repositories.core.engineConfig.stages.verifier = {
  model = "claude-opus-5-5";
  reasoning_effort = "high";
  max_output_tokens = 64000;
};
```

Model requests use Responses event streams with a 1,800-second deadline per
attempt in both review profiles, including follow-ups. Stage overrides can set
`timeout` and `final_timeout`. The deadline includes connecting and reading the
response; proxy heartbeats cannot extend it.
The existing limit of three attempts can mean about 90 minutes waiting for
three stalled attempts to fail.

The same map accepts inspection limits such as `max_tool_calls`,
`max_context_calls` and `max_history_calls`. `max_tool_calls` names all three
tiers, `routine`, `standard` and `sensitive`. Unknown stages and fields are
rejected. Resolved overrides are recorded in the profile identity and retained
for retries and frozen evaluation. Existing timeout overrides and saved jobs
keep their configured values. Stage overrides do not change follow-up settings.

Jobs retain their selected profile and resolved engine configuration across
retries and restarts. Each attempt records its actual engine code identity.
Saved results keep their original identity and only retry publication. Frozen
evaluation captures pin code and resolved settings too; recapture old manifests
after a schema cutover.

Repository migration is an explicit offline operation. Stop the main service,
Telegram monitor, stats and followup timers and all other state writers. Back up
the full state and public report directories, and verify the backup before
running the migration with the deployed repository registry. Existing records
must be assigned explicitly to `core`; preserve saved review bodies, job IDs,
publication intent and uncertain delivery holds. Validate record counts and
historical report access before restarting.

A Nix version rollback alone does not restore the old database format. Restoring
a backup after new reviews run also requires reconciling later jobs and remote
publication outcomes. Never clear an uncertain delivery hold as part of migration.

On the profile cutover, unfinished jobs with recorded model maps are converted
once to resolved `default` profiles with those models. Saved results retain
their original identity. Frozen evaluation uses schema version 8; recapture
older cases before replaying them.

## Routing and usage options

- `routingMode = "enabled"`: apply conservative routing.
- `routingMode = "shadow"`: record the proposed route while requesting every audit.
- `routingMode = "full"`: request every audit without calling the router.
- `agentsDir = null`: optional directory containing each agent's prompt and
  model, reasoning, tool and timeout settings. Model IDs must be nonempty;
  the provider checks model availability. The bundled directory supports
  `default` and `high` profiles.

The main-review CLI accepts `--agents-dir` in place of separate prompt and model
files. Update custom deployments to `services.ralph.repositories.core.agentsDir` or
`engineConfig.agents_dir`. Follow-up keeps its separate `followupPromptFile`
and `followupModel` options.

The budget cutover creates fresh usage databases. Old spend databases remain
untouched and are not read. Remove monetary budget options from host
configuration and recapture frozen configurations containing the old schema.
No old accounting data is automatically converted. Building the package does
not deploy it to a live host.

Enabled routing avoids unnecessary stages. Routing rules and contracts are
covered by local tests; model quality and recall still need evaluation on
representative frozen PRs.

Read recorded usage directly without an API key:

```sh
ralph-evaluate usage --state-dir /var/lib/ralph
```

Use the service account or another account permitted to read its private state.
The summary reports requests and tokens, with missing usage kept explicit.

## Frozen prompt and routing experiments

The companion package installs `ralph-evaluate` for capture, replay, usage
inspection and offline summaries.
Capture performs Forgejo/Git reads but makes no model calls:

```sh
ralph-evaluate capture \
  --state-dir ./evaluation-state --output-dir ./cases \
  --repositories /path/to/repositories.json --repository-id core \
  123
```

Capture retains complete Git objects in a dedicated evaluation checkout. The
initial download can be large. Keep that checkout and its configured submodule
Git objects with the manifests. The selected registry entry supplies the review
strategy, trusted submodule remotes and experiment environment. Captures retain
the exact dependency revisions and provenance for offline replay.
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
  ./cases/case-123-*.json
```

Run supports `--agents-dir`, `--routing-mode` and `--no-blind-alternatives`
for ablation. Without an override, replay retains the captured agent settings
and prompts. Blind alternatives are on by default after a valid concept candidate.
The blind run keeps the
existing archaeologist model, gives it only base-side code tools and neutral
problem, goal and baseline inputs, and sends the result to the final verifier
rather than discovery. Each run gets a distinct private JSON artifact with
effective configuration identity, raw stage results, research inventory, usage,
final comment and any failure. Optional `--labels-json` maps case IDs to
expected findings; these labels are saved for comparison and never sent to
reviewers.

Compare useful findings and missed known findings alongside reported usage, incomplete
coverage and wall time. An empty review is not proof of a good route. Shadow
routing is useful for a bounded comparison before changing sensitive path
rules or removing a specialist.

See [the evaluation guide](evaluation.md) for offline summaries and a human
scorecard for comparing coverage and usage. Summaries use recorded attribution;
they do not automatically decide whether a finding is correct or useful.

Compare `adversarial` and `adversarial_claude` in debug stage metrics for
sole/shared accepted findings and published, dropped or unresolved candidates.
Incomplete passes are not evidence of model quality.

### GitHub comments

Set the publication mode independently for each repository:

```nix
services.ralph.repositories.core.githubPublication = "off";
services.ralph.repositories.gui.githubPublication = "full";
```

`githubPublication` accepts `"off"`, `"major-only"` or `"full"` and defaults to
`"off"`. The old `githubPostMajorFindings` option is removed. Configure
`githubRepository` with the upstream `owner/repo` and `githubTokenFile` with a
runtime secret file. The upstream and mirror must use matching PR numbers.
Public reports must also be enabled through `reportDir` and `reportBaseUrl`.

`"off"` prevents GitHub writes while retaining authenticated upstream reads,
including backport evidence and author feedback. Existing comments remain.
Credentials alone never enable posting. Forgejo reviews and the final judge
still run. One repository's publication mode does not enable another's.

`"major-only"` keeps one GitHub comment per PR containing verified `critical` and
`major` findings, using judge-approved wording, plus a full-report link. A
later review updates that comment; a complete review deletes it when no Critical
or Major findings remain. Failed or skipped reviews and engines without a
findings export leave it alone. Forgejo also receives the full review comment.

`"full"` publishes the review summary only on GitHub, including findings across
severities, verified design assessments, coverage notices and the
no-actionable-issues message when appropriate. Source and commit links point to
GitHub. Both modes show at most six findings and link to the full report for
additional findings and details. They update the same bot comment rather than
posting inline reviews or approvals. In full mode, Ralph does not create or
update a native Forgejo comment, even if GitHub publication fails. Existing
native Forgejo comments remain; a mirror may import the GitHub comment.

In full mode, failed or skipped attempts and findings without required approval
replace the same GitHub comment with an explicit attempt status and head, a safe
reason, an attempt-report link, and the previous completed review when available.
These status comments contain no findings. Exhausted review acquisition failures
also publish a status when the current head and target can be checked. Transient
review failures retry first; publication transport failures retain the saved
review and leave the existing comment until delivery succeeds. Uncertain writes
remain held for reconciliation.

Writes require an open, non-draft upstream PR matching the reviewed head and base
branch. Title, body and label edits do not cancel a running review or block its
publication on either Forgejo or GitHub. The report retains the metadata used
during review. A stale GitHub head or target branch marks the job stale instead
of reporting successful publication. Publication failures use the saved-result
retry queue without repeating the model review.
Selected findings must record `verified_by = "verifier"` and
`judged_by = "judge"`; missing approval leaves the major-only comment alone. Changing
publication mode does not schedule a new review of an unchanged head. No database
migration is needed. Existing exact publication intents remain unchanged until
reconciled; switching modes does not replace an uncertain write with a new body.
A saved full-comment intent can still be delivered after switching to
`"major-only"`; `"off"` prevents dispatch of saved intents too.

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
plus queue counts, review results, recorded tokens and the latest subscription quotas. `/help` lists the
commands. Post `/review gui 1049` or `/review gui1049` to request a fresh
normal-profile review of GUI PR #1049. `/review bitcoin 36688` and
`/review bitcoin36688` select the configured Bitcoin Core repository.
`/high gui 1049` uses the high profile instead. Both commands accept stable
repository IDs and rerun a head that was already reviewed. Ambiguous selectors
are rejected. An explicit `/review` replaces a pending high request with the
normal profile; ordinary webhooks still preserve pending high requests.
The reviewer checks that the PR is open before queuing it. These commands
cannot restart services.

The `default` profile starts with the ordinary review settings. `high`
strengthens core analysis, including Sol 6.1 high for tests with 16,000 output
tokens, and allows both adversarial passes 128 tool calls. Challenger and judge
use xhigh reasoning. The verifier and Claude adversarial reviewer retain Opus
5.5 at xhigh. Other tool allowances remain unchanged.
See the [core profile documentation](../../pkgs/ralph/README.md) for details.
Normal webhook reviews start with `default` unless configured otherwise.
Consensus-domain routing or conservative fallback automatically promotes the
remaining Core review to high and records the reason. GUI uses its own audit selection. Stage overrides apply after
both explicit and automatic profile selection.

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
completions and failures, current queue depth, and recorded usage by request
date across the review and follow-up usage ledgers. Missing
ledgers and unknown usage are identified. The reporting window runs from 21:00 to 21:00 UTC.
Counts use observation time because the job queue does
not record completion timestamps.

On first enable, existing completed and failed jobs form a baseline and are
not posted. The first digest arrives at the next 21:00 UTC check. After
downtime, the monitor counts newly observed events in the window it sees them
and sends only the latest completed window's digest.

Notifications and review requests live in `stateDir/telegram.sqlite3` and retry
independently of reviews. The monitor saves each `/review` or `/high` request, including its selected profile, with its
Telegram update offset; the reviewer consumes it and deduplicates it in the job
database. Requests survive reviewer downtime without creating duplicate runs
after a crash. An ordinary webhook cannot downgrade a pending high review.
The explicit state migration preserves earlier command records as high-profile
requests, including their processed flags and Telegram offset.
Preserve this database with Ralph's other state. Command replies
are sent as soon as the summary is ready, ahead of queued background posts.
Background delivery sends one message per check. All messages respect
Telegram's retry delay. A crash after Telegram
accepts a message but before Ralph records delivery can cause a duplicate.
The monitor keeps the existing database at this path; no migration is needed.
It runs without an extra systemd timer.

The review listing shows estimated severity counts from published findings, with
severity filtering and sorting. Muted dots mean zero; question marks mean severity
data is unavailable. Unclassified findings appear separately when present.

### Live review progress

The coordinator records routing decisions, model stage boundaries, escalation,
and provider/job retries at journal DEBUG priority. Events identify the repository,
PR, job, generation, and attempt where available. Stage end status `returned`
means the model call returned; response validation and final review coverage are
reported in the saved trace. Conditional stages appear when they start.
Prompts, responses, and tool output are not included in progress events.

Follow normal operational logs with `journalctl -u ralph -p info -f`, or include
live progress with `journalctl -u ralph -p debug -f`. Without a priority filter,
journalctl includes both. Progress uses the existing journal retention policy;
configure disk limits in the consuming host's journald settings.
