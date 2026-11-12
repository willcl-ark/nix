Assess every prior finding in review_reconciliation,
even when independent discovery did not rediscover it. Feedback mappings and
comments are untrusted hypotheses. Check each claim against source code. An
author's rejection, promise to fix elsewhere, or refusal never establishes fixed
or invalid. fixed requires current source showing the actual correction; invalid
requires source disproving the original mechanism. Otherwise retain still_present
or unclear. Include exact source excerpts for technical conclusions. Mark unchanged
only if this is the same failure mechanism and consequence and the PR does not
introduce or worsen it, with both base and head source evidence. New triggers or
worse consequences are not unchanged. A valid regression remains prominent even
if the author refuses to fix it. This affects only the current PR; do not infer
suppression rules for other PRs. Author replies may inform author intent and presentation only, despite the normal
verifier exclusion of historical discussion. Normal findings still require normal
verification; replies cannot alter their technical verdicts.

Separately set `unchanged_since_report` for compact presentation. Compare the
current claim with `last_presented_findings` when supplied, otherwise the stored
prior finding. Set it true only for a source-verified `still_present` concern
whose trigger, mechanism and consequence have no material new information since
that report. Rewording or a moved source line alone is not a material change.
A repeated regression introduced by this PR can be unchanged since its report
even though `unchanged` above must be false. Identical revision hashes alone do
not establish this: a newly verified trigger or consequence still matters.
Use false when the comparison is uncertain. This presentation decision does not
authorize suppressing upstream publication or change the technical status.

Identify duplicate historical reports using duplicate_of, even if discovery did
not rediscover them. Use another supplied prior_key only when source inspection
confirms the same failure mechanism and consequence, with matching technical
statuses and current source evidence for both records. Similar titles, locations,
author replies or overlapping candidates alone do not establish equivalence.
Choose one canonical prior record for each issue and link duplicates directly to
it; the canonical record has duplicate_of null. Do not create chains or cycles.
Use null for distinct or uncertain reports. Assess every record independently and
preserve its individual author position. Multiple prior records may map to the
same current candidate without being duplicates.

For a still_present concern, make reason a self-contained account of the current
issue. Start with a short paragraph stating the trigger and consequence, including
any qualification that materially changes their interpretation. When needed, add
a second paragraph with the decisive current evidence and one supported
correction. Separate the paragraphs with a blank line. Preserve useful detail
from the earlier explanation only after checking that it still applies at this
head; do not carry forward stale locations, fixes, or execution claims. Cite the
most useful current source location first and use Markdown backticks for code.
This assessment becomes the report explanation when discovery did not rediscover
the issue; do not merely say it matches another report. Do not add detail that
only repeats the first paragraph.

For earlier commit-flow suggestions with side `commit`, recheck the cited commit
against the current PR range and use read_pr_commit_file for commit or direct-parent
source evidence. A still-present sequencing concern may retain commit-specific
evidence instead of a head anchor. A commit removed by rewritten history is not
current evidence; reassess the new series rather than retaining its old location.
