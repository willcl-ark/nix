# ralph-service

`packages.<system>.ralph-service` provides the Forgejo webhook service and the
consumers of the [local Ralph review engine](../ralph/README.md). It depends on
the exact core package and imports its `run_review` function directly.

The package installs:

- `ralph-service`, the webhook receiver, queue workers and explicit state converter.
- `ralph-evaluate`, frozen capture/replay, usage inspection and offline summaries.
- `ralph-followup`, background assessment of previously published findings.
- `ralph-telegram`, independent Telegram publication, monitoring and channel commands.

The `ralph` command performs a local review and accepts no service flags.
The service uses one repository registry, queue and worker pool. Configure NixOS
under `services.ralph.repositories.<id>`; the module generates the registry.
Manual commands use the same JSON file:

```sh
ralph-service --state-dir /path/to/state --repositories /path/to/repositories.json \
  --openai-key-file /run/secrets/proxy-key \
  --report-dir /path/to/public-reports --report-base-url https://reviews.example.org/traces
```

The registry is an object keyed by stable IDs such as `core` and `gui`. Each
entry contains `origin`, `repository`, `forgejo_api`, `repository_url`,
`bot_login`, `webhook_secret_file` and `forgejo_token_file`. Credentials are
runtime file paths, never secret contents. `engine_config` holds prompt/model
settings; `strategy` selects `core` or `gui` independently of default/high compute
profiles. `max_workers` caps that repository within the shared `--workers` limit.

Set `core.github_publication` to `"off"`. GUI can set
`github_repository` to `bitcoin-core/gui-qml`, `github_token_file` to its runtime
credential path, and `github_publication` to `"full"`. Only that repository then
publishes full review summaries upstream. Each repository independently accepts
`"off"`, `"major-only"` or `"full"`; the default is `"off"`.

GUI alone sets `submodules` to
`{"bitcoin": "https://github.com/bitcoin/bitcoin"}` and `experiment_shell` to its
Qt environment launcher. Other repositories default to no submodules. Preparation
fetches the configured trusted remote and uses each GUI revision's exact pin;
PR-controlled submodule URLs are not followed. Forgejo mirror sync needs no
submodule update command.

See the [module guide](../../modules/ralph/README.md) and
[evaluation guide](../../modules/ralph/evaluation.md).

Core imports and defaults live under the core package's `libexec/ralph`.
Companion code and launchers live under `libexec/ralph-service`; wrappers add the
core Python root without copying its source. Core has no companion dependency.

## Service and review lifecycle

```mermaid
flowchart TD
    event["Forgejo PR opened, reopened or synchronized"] --> webhook["Webhook server: validate signature and repository"]
    webhook --> hints["Durable reconciliation hints"]
    hints --> queue["Read authoritative PR state, then coalesce desired work"]
    queue --> workers["Review workers: default 3 concurrent PRs"]
    workers --> collect["Fetch current branch or pinned historical base and collect merge-base/head snapshot"]
    collect --> existing{"Same base and head already reviewed?"}
    existing -->|yes| done["Complete without model calls"]
    existing -->|no| pipeline["Route, assess concept, discover, verify and edit"]
    pipeline --> saved["Save result before publication"]
    saved --> report["Optional public HTML and JSON report"]
    report --> publish["Create or update one bot comment per PR"]
    pipeline --> trace["Private trace: outputs, tools, usage and attribution"]
    saved --> stats["Static statistics from jobs and usage ledgers"]
    workers --> stale["Changed head: supersede and enqueue latest head"]
```

Only one attempt runs per repository and PR. Each repository has its own checkout;
fetch and snapshot preparation share that checkout's Git lock;
model calls run outside it. Each attempt pins its Git objects with separate refs.
PR target, head and local generation checks guard publication. Webhooks and
stale-head refreshes record durable hints; the coordinator resolves current PR
state before advancing the desired generation. Reconciliation continues while
review workers are busy. Same-head metadata changes do not automatically request
another paid review. Publication retries reuse saved results without another
model run. Transient transport failures have bounded job retries. Oversized inputs use a changed-file manifest and diff
tools; if the PR prelude and manifest still exceed 200,000 bytes, review is skipped.

Review outcome and delivery status are separate. A failed verifier marks the
review as failed in its saved result, public report, logs and statistics. The
service still delivers verified findings and review history, then completes the
queue job so publication retries do not run the models again. A failed review
cannot create, update or delete the upstream GitHub major-findings comment.
Public stage diagnostics include the bounded, redacted provider error message
and HTTP status, without response bodies or headers.

Sources: [service.py](ralph_service/service.py),
[jobs.py](ralph_service/jobs.py), [repository.py](ralph_service/repository.py).

The HTTP listener also serves per-PR PNG status dots at
`/assets/<owner>/<repo>/<pr>.png`. They are Bitcoin orange (`#f7931a`) when the
latest queued generation has published findings or a conceptual concern,
including feedback from partial reviews. All other results are grey. Neither
colour indicates approval or a pass/fail verdict. Status comes from the existing
job store without model or Forgejo API calls. See the module documentation for
proxy setup.

## Uncertain publication recovery

Before sending a comment write, Ralph saves its destination, operation and exact
body in `jobs.sqlite3`. Delivery retries reuse those bytes. A timeout or restart
can leave the remote outcome uncertain. Ralph checks bot-owned comments on both
publication destinations and holds further writes for the PR while an earlier
write remains unresolved, including writes from newer review generations.
Duplicate bot markers require operator investigation.

Stop all state writers and take a verified backup before the repository schema
migration. Explicitly assign existing state to `core`:

```sh
ralph-service --state-dir /path/to/state --repositories /path/to/repositories.json \
  --migrate-state --legacy-repository core
```

Migration preserves job IDs, saved reviews, exact publication intent and existing
uncertain delivery holds. Startup refuses unmigrated state. Do not run an older
binary against the upgraded database. Restoring a backup also loses work accepted
since that snapshot and cannot undo remote writes; reconcile intervening work
before restarting.

Stop the coordinator before inspecting or releasing holds. These commands acquire
its state-directory lock and need no provider credentials. Listing includes all
repositories; release requires an explicit repository ID:

```sh
ralph-service --state-dir /path/to/state --repositories /path/to/repositories.json \
  --list-held-deliveries
ralph-service --state-dir /path/to/state --repositories /path/to/repositories.json \
  --repository-id gui --release-delivery 123 --confirm-no-pending-write
```

Release only after establishing that outstanding writes can no longer arrive.
An absent comment alone does not establish this. Release permits delivery retry
with the saved bytes and can reset the latest saved job's exhausted retry budget;
it does not rerun analysis. Local model cancellation closes the connection but
cannot establish that remote inference or billing stopped.

## Agents, comment formatting and follow-up

To tune a main-review prompt, model or tool limit, edit the corresponding
directory under [core agents](../ralph/agents/). Each agent keeps its prompt and
TOML settings together. Comment templates live under
[final_comment](../ralph/final_comment/). Validate edits from the repository root:

```sh
python3 pkgs/ralph/review.py check-agents
```

This check needs no credentials or review target. The service uses the bundled
agents by default. Set a repository's `engine_config.agents_dir` in the registry,
or `services.ralph.repositories.<id>.agentsDir`, to select a complete alternate
agent directory. `ralph-evaluate capture` and `run` also accept `--agents-dir DIR`;
replay keeps the captured settings unless explicitly overridden.

For the current engine, mirror comments show at most six current verified
findings, including historical issues confirmed as still present at this head.
They sort by severity, with current findings first on ties.
Each shows its title, impact summary and source location. Additional findings
link to the full report. Earlier unresolved concerns and incomplete coverage
receive short notices. Finding explanations, alternatives and full review
history remain in saved reports and structured exports. The comment footer
links the reviewed revision and report.

The follow-up command loads its own `audits/addressed_findings.md` and default
model. Use `ralph-followup --prompt-file FILE --model MODEL` to override those
settings. Main-review agent settings do not affect follow-up.

## State and upgrades

Keep `jobs.sqlite3`, `telegram.sqlite3`, private traces, every repository checkout
and retained refs, public reports, and all of `followup/`. Back up the complete
state root, including `repos/`, and the complete public report root. Preserve
follow-up initialization metadata, `assessments.sqlite3`, its checkout and its usage ledger.

New requests are recorded in fresh `usage.sqlite3` and `followup/usage.sqlite3`
databases. Ralph does not read, convert or delete the old spend databases.
There is no price table, monetary admission limit or reservation accounting.
Each request attempt records its outcome and reported usage; missing token or
cache counts remain unknown. The stats page separates these incomplete counts
from reported totals.

Queued and frozen configurations that contain the old budget schema need to be
recreated. No automatic conversion of old usage data or live host deployment
is performed by building these packages.

Pass `--repositories /path/to/repositories.json` to `ralph-evaluate stats` and
`python -m ralph_service.backfill` to attach configured repository names and URLs.
This option is optional for historical/offline presentation. Backfill remains a
dry run unless `--apply` is supplied. Reports and stats label repository identity
and provide per-repository views; Core's existing report URLs remain valid.

The stats command can also take `--proxy-management-url` and
`--proxy-management-key-file` together to collect passive Codex and Claude quota
snapshots through CLIProxyAPI v8. Use a separate management key, not an inference
client key. Only the stats process needs it. Snapshots expose no account identity,
OAuth token or credential path. Quotas include usage outside Ralph, remain
separate by window, and show reset times and collection time. Missing readings
are unavailable, never zero. No inference requests or usage-queue reads are made.
The bundled proxy UI does not need a separate frontend deployment.

The `stats/limits/` page groups saved stage observations by agent, model,
profile and configured cap. It shows usage at the cap, confirmed interruptions
and affected review links. Older results without telemetry
remain uninstrumented. Output-token usage is measured per response; web-search
cap usage is observable but provider-side blocking is not. These counts help
choose limits to investigate, rather than measure review quality.

Queued reviews retain their resolved profile and prompt settings across retries
and restarts. The running engine code may update after a deployment; its hash
is recorded when the worker claims the review. Saved results retain their
original identity and retry publication without running the engine again.
Existing unfinished reviews migrate once into the default profile, preserving
their configured models.

Switch the companion package, its core dependency, module commands and custom
prompt/model configuration together. Before a deployment, stop timers and
services and take SQLite-consistent backups, retaining refs and reports too.
Hydrate each repository's Git store before starting local-only snapshot reads. Start
one companion against the existing state and resume timers after startup succeeds.
No deployment is performed by building these packages. A package rollback does
not undo published comments or recorded model requests.

Repository registry fields `github_repository` and `github_token_file` enable
authenticated upstream metadata reads independently of posting.
`github_publication = "major-only"` maintains one upstream GitHub comment with
`critical` and `major` findings and a public report link, alongside the native
Forgejo review. `"full"` publishes only on GitHub, including other finding
severities, verified design assessments, coverage notices and the
no-actionable-issues message when appropriate. Source
links point to GitHub. Both modes use a six-finding limit; the full report retains
the rest. Full mode leaves existing native Forgejo comments untouched and does
not fall back to Forgejo if GitHub publication fails. Posting requires public
report configuration and credentials.
Later reviews update the same comment. In major-only mode, a complete review can
delete it when no Critical or Major findings remain; incomplete reviews cannot
delete the comment. Failed or skipped reviews do not post in either mode.
`"off"` leaves existing comments untouched. No database migration is needed;
existing exact delivery intents are preserved until reconciled. The old
`github_post_major_findings` boolean is rejected; replace it with the new mode.
Upstream PR numbers must match the mirror. Writes require an open, non-draft upstream PR
matching the reviewed head and base branch. Metadata edits do not block
publication. GitHub credentials are separate from Forgejo credentials and model
tools.

Run `ralph-telegram` separately from the review daemon with `--state-dir`,
`--report-dir`, `--report-base-url`, `--repositories /path/to/repositories.json`,
`--telegram-token-file /run/secrets/telegram-token` and
`--telegram-chat-id @channel`. Use a numeric channel ID for a private channel.
The monitor reads review state and recorded usage, publishes completed reviews, sends
grouped terminal failure alerts, and delivers a daily digest at 21:00 UTC.
It continues running when the review daemon stops.

Each review post contains the PR title, current finding counts by severity,
and links to the PR and full report. Review bodies are never included. Long
titles are shortened to fit Telegram's 4,096 UTF-16 code unit limit while
preserving the counts and links. A new exported finding with severity exactly
`major` adds `🚨 Major finding`.

Post `/status` in the configured channel to see service states, queue and
result totals, recorded token usage and the latest subscription quota snapshot. `/high gui 123` requests a fresh review
of GUI PR #123 using the high profile. The repository ID is required. It increases
the reasoning effort and models of the harder core review stages. `/help` lists the
commands. Commands are channel-only. All channel administrators may issue commands.
Anonymous posts rely on Telegram's administrator posting permissions;
identified senders must be current administrators. The monitor verifies bot
posting permission instead of trusting displayed signatures. Direct messages
are ignored. Replies are sent as soon as the summary is ready and take
priority over background posts, while honoring Telegram's retry delay.

High requests are saved with the Telegram update offset and reply in
`telegram.sqlite3`. The review daemon consumes them and records request IDs
transactionally with the job, so a restart cannot duplicate a command. Normal
webhooks and head refreshes preserve a pending or running high review. The
monitor can accept commands while the review daemon is stopped. A second
reply confirms queueing or explains why the PR could not be queued.

Normal webhook reviews use the default profile. Set the repository's `engine_config.profile` to
`"high"` to select high for ordinary reviews too. Custom engine modules do not
support `/high`.

Use a bot token from `@BotFather`, grant the bot channel administrator
permission to post, and keep its token in a runtime secret file readable by
the service user. The monitor polls Telegram; no webhook or inbound port is
needed. See the
[module guide](../../modules/ralph/README.md#telegram-publishing-and-monitoring)
for NixOS setup and delivery behavior.

## Backport reviews

Backport PRs receive the core's fidelity and release-branch review. The service
collects labels, title/body and the target branch before checking for a reusable
result. It resolves upstream PR commit lists and original objects, including
abbreviated `Rebased-From` trailers, then pins the acquired history for the review.
Unavailable or ambiguous provenance appears in the report as a limitation.

Reuse includes a fingerprint of review metadata and backport evidence alongside
the engine identity and base/head. Metadata is checked again during review and
before publication. Publication retries reuse the saved result when its reviewed
context remains current. Installing the feature does not enqueue historical PRs.

The report includes the verified backport assessment separately from inline code
findings. Missing whole commits can therefore be reported without fabricating a
source-line location. See [core behavior](../ralph/README.md#backport-reviews).

Label/body edits alone do not enqueue reviews. Use the existing explicit review
request to re-review an unchanged head after editing its backport scope.

Frozen cases use schema 7 and retain the backport evidence and available upstream
objects for offline replay. Recreate older case manifests before evaluating with
this version; replay rejects changed evidence or missing source pins before any
model request.

## PR-local author feedback

Before a re-review, the service gathers earlier completed findings for that PR
and carries their saved review memory forward. New results record repository/PR
scope explicitly; legacy results use the service's dedicated repository database.
No history is shared between PRs. Comment edits alone do not enqueue new reviews.

For repositories configured with `github_repository`, author identity comes from
the upstream GitHub PR's numeric user ID. The service captures that author's issue
comments, inline replies and review bodies from GitHub. Standalone replies on the
Forgejo mirror are not included in this mode. Native Forgejo repositories use
Forgejo numeric user IDs; imported author names alone are insufficient. Reads are
bounded and record omissions. First reviews perform no feedback discussion reads.

Author comments are untrusted text. Their exact captured bodies, IDs, URLs,
timestamps and hashes accompany the previous findings and a bounded revision
diff. “Addressing elsewhere” cannot erase an issue: the core verifier must check
the code, and unresolved history remains in reports and status badges even when
there are no new findings. A collapsed entry is still unresolved.

Same-head reuse checks revision and review metadata before feedback acquisition,
so new replies cannot trigger duplicate model runs. The packet fingerprint is
saved separately with the result. Publication retries use the
saved result and frozen packet without another model run. Frozen
captures retain the same evidence. Historical captures exclude older results
without a trustworthy completion timestamp; ordinary captures retain legacy
history. Feedback acquisition failures retain prior findings for reconciliation
without treating unavailable replies as author acceptance or rejection.
