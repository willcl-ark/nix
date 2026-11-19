# Ralph service operations

See [service setup and recovery](README.md) for the repository registry, jobs,
publication holds and state backups. NixOS settings belong in the
[module guide](../../modules/ralph/README.md).

## Reports and statistics

### Finding feedback

Report readers can vote on whether each finding was useful and optionally leave
an explanation of up to 2,000 characters. The browser remembers a random token
so readers can change or remove their feedback. Clearing browser storage loses
that ability. Counts represent browser votes, not authenticated people.
Explanations are private to maintainers and the originating browser; they are
not public comments and do not change review findings or trigger model calls.

The coordinator stores feedback in `STATE/feedback.sqlite3`, separately from
saved reports, keyed by repository, run and finding, with the reviewed SHA.
Keep this database in private state and include it in state backups. Serve the
same-origin `/feedback/*` route through the coordinator and allow
`connect-src 'self'` on reports, as shown in the module guide. Refresh existing
reports with the backfill command below to add the controls without rerunning
reviews.

Run triage locally as the service account:

```sh
ralph-feedback --state-dir STATE list
ralph-feedback --state-dir STATE list --html /private/path/feedback.html
ralph-feedback --state-dir STATE review ID REVISION
```

The list and HTML contain unreviewed feedback and links to the exact report
findings. The HTML file is private; do not copy it into the public report tree.
Use the ID and revision from the listing to mark an entry reviewed. If the voter
changes it meanwhile, acknowledgement fails so the new explanation is not
missed. Changed feedback returns to the queue; withdrawn feedback is removed.
Reviewing feedback does not change votes or counts. Curate confirmed examples
manually before using them to evaluate review changes.

When Telegram monitoring is enabled, Ralph sends a reminder once per UTC calendar
week if unreviewed feedback exists. It queues the first reminder when pending
feedback is observed, then at most one in each subsequent week, starting Monday
00:00 UTC. Empty queues produce no reminder. The message includes only the
pending entry count and local triage command, never explanations or finding
titles. The existing durable Telegram outbox deduplicates reminders across
restarts and retries delivery. Counts reflect the time the reminder was queued.

Feedback records contain the latest vote and explanation, not an edit history.
Edits replace previous text and withdrawal deletes the record. Reviewed entries
remain stored but are omitted from the pending list.

### Report rendering

New reviews cite source using citation IDs issued by source-reading tools. Ralph
resolves IDs within the current review and saves the file, revision, lines and
exact source text with the evidence. Later stages and repair passes can reuse
same-review IDs; prior-review evidence requires fresh source reads. Unknown IDs
still require repair. Valid citations do not establish that the review's
reasoning is correct. Saved reports remain self-contained and existing reports
remain readable without rerunning reviews.

The Review report contains approved findings, their explanations and qualifications,
and a coverage link into the Run archive. Failed verification remains explicit. The Run archive contains preliminary agent replies, decisions,
coverage diagnostics, retained history and usage. Intermediate claims in the
archive are not published findings. Both views use one saved public JSON record;
private request payloads, configuration and private traces remain private.

Set `reportBaseUrl` to the URL serving `reportDir/reports` and `archiveBaseUrl`
to the URL serving `reportDir`. Each run has matching HTML paths beneath these
bases. Publication retries recover missing pages from the saved JSON without
repeating inference. Native Forgejo publication can continue without a report
link if saving fails; GitHub comment creation and updates require a Review
report URL.

Reports show findings before backport assessments and supporting evidence.
Each finding has a visible Link control that targets the start of the finding.
The fragment uses its saved finding ID and works without JavaScript. Refresh
existing report HTML with the offline backfill to add these links.

Report source and commit links use the configured `github_repository` upstream
when present. PR-discussion links retain their original repository URL. New
snapshots save the upstream mapping; refreshing older snapshots with
`--repositories` supplies it without modifying their JSON.

PR comments reuse approved finding summaries. Telegram sends a notification
with metadata and a report link, never review prose. Neither delivery channel
rewrites the judge's wording.

Challenger outcomes connect candidates to challenge, source verification and
publication decisions. These counts measure agent agreement, not correctness or
confirmed delivery. Their statistics links point to the Run archive.

History remains at `archiveBaseUrl/<PR>/` for Core and
`archiveBaseUrl/<repository-id>/<PR>/` for other repositories. Each entry links
to the Review report and Run archive. Add `?commit=<SHA>` to filter by a full
commit hash or prefix. The archive root has the existing search form. History
includes runs that were never posted, counts each run once, and labels reports
unavailable until their human pages have been generated. History and the Reviews
table count the findings shown in the Review report: the findings approved in
that run. Earlier findings appear only when the run approves them again;
unresolved ones stay in the archive. Agent attribution and publication metrics
count exported findings. When this counting changes, bump `VERSION` in
`stats_cache.py` so cached counts are rebuilt.

Before switching links to `/report`, preview the offline backfill using the
installed service Python environment:

```sh
python -m ralph_service.backfill --state-dir STATE --report-dir PUBLIC \
  --report-base-url https://reviews.example.org/report \
  --archive-base-url https://reviews.example.org/traces
```

Add `--apply` to generate both views and refresh navigation from saved public
snapshots, or recover missing snapshots from completed jobs. Add `--refresh`
to regenerate existing pages with the current renderer from their saved JSON. This makes no model
calls or comment updates. Human pages use structured approved findings; the original Markdown and raw
coverage messages stay in the archive JSON. Inspect reported failures before cutover. Rebuild
statistics with `--full` afterward. Rendering changes apply to eligible saved
records through this refresh, including source links, grouping and outcome text.
PR titles appear when saved in the public record; refresh does not fetch missing
metadata or rewrite approved prose. Snapshots saved before the current format
are reported as errors (a missing `base` or `summary` key) and keep their old
pages, so style or link changes reach those pages only through an in-place
edit of the HTML. Back up the files you edit, and change only markup or link
targets, never recorded trace text. Judge and reconciliation prompt changes
affect future reviews only. Retained findings link to their earlier report for
context rather than treating an old explanation as newly verified. Deploy the engine, service, module options and
static routes together; old workers retain their pinned configuration and should
finish before the one-time presentation backfill. The template schema changes
without compatibility handling. Publication retries holding an old template
snapshot may fail after cutover and need a newly requested review; no automatic
replay or saved-result migration is performed.

A failed feedback stage leaves the remaining review stages running without its
mapping. For `InvalidReview`, inspect `validation_error` in the saved stage
metadata or the trace coverage limitation. Unknown or duplicate references name
the field and offending IDs. The mapper's input excludes internal source-anchor
IDs and its response schema permits only references from that request. Validation
failures are not retried or repaired by translating IDs.

Statistics read durable jobs and usage ledgers without model requests. Shared
findings credit each contributing agent, so counts overlap. Concept assessments
have separate counts; archaeology does not compete in the finding leaderboard.
Verifier acceptance is not human validation or a recall measurement. Retries
increase request counts without duplicating final findings, and summed model
time is not elapsed review time.

The Limits page at `stats/limits/` distinguishes usage at a cap from recorded
interruptions, with links to affected reviews. Missing telemetry remains
uninstrumented. Reaching a cap does not establish that more allowance would
improve a review. Usage totals describe Ralph's requests; subscription quotas
also include other tools using the same accounts.

## Rebuilding statistics

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

## Requesting another review

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

Addressed-finding assessments cite IDs attached to captured source and path diffs.
IDs are scoped to one finding and comparison, and accepted assessments retain
full captured text and revision metadata in source_evidence. Definite outcomes
require current source or a source-to-new diff; unavailable or insufficient
context remains unclear. Capture limits and diff truncation are explicit. This
changes new assessment responses only; existing stored assessments remain readable.

Historical document and discussion reads issue source IDs from structured adapter
attribution. Historical claims reference those IDs; saved reviews retain the
resolved URLs, inspected passages, provenance and retrieval limitations. Search
hits and URLs mentioned in source prose do not create citations. IDs are local
to a model stage and are regenerated for replay. Frozen research captures now
use schema version 3 to store attribution and notices separately from tool text;
version 2 captures must be recaptured before replay. Existing saved reports
remain readable and public history sections continue to show resolved links.

Finding locations are selected with review-local anchor IDs returned by diff
reads. Only added and deleted source lines actually returned by the tool receive
IDs. Saved findings retain resolved paths, sides and line numbers. Commit-flow
anchors use the commit tree or its first parent; renamed files use separate old
and new paths. Binary and mode-only changes do not fabricate textual anchors.
Configured dependency diffs remain supporting evidence only; they do not issue
publication anchors because reports link locations in the reviewed repository.
