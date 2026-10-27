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
