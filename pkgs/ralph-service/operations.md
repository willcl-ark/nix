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

### Report rendering

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
table count the findings shown in the Review report, including current-head
confirmed retained findings. Agent attribution and publication metrics continue
to count exported findings, rather than crediting retained findings again.

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
metadata or rewrite approved prose. Judge and reconciliation prompt changes
affect future reviews only. Retained findings link to their earlier report for
context rather than treating an old explanation as newly verified. Deploy the engine, service, module options and
static routes together; old workers retain their pinned configuration and should
finish before the one-time presentation backfill. The template schema changes
without compatibility handling. Publication retries holding an old template
snapshot may fail after cutover and need a newly requested review; no automatic
replay or saved-result migration is performed.

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
