# Public finding format

Write each finding for the author using these fields:

- `title`: a short description of the defect or limitation. Describe what goes
  wrong, rather than starting with an instruction to fix it.
- `summary`: one short sentence explaining why it matters. Include any
  qualification needed to avoid overstating the impact.
- `body`: a self-contained explanation of the trigger, mechanism, consequence,
  and one sufficient correction when established. The public comment puts this
  explanation in a collapsible section below the title and summary.

Aim for one paragraph in the body. Use short sentences rather than compressing
several ideas into one sentence. Preserve qualifications that affect correctness
or severity. A small code example or another paragraph is fine when needed for
clarity. Avoid repeating the summary or listing several fixes when one is
sufficient. Brevity must not remove necessary detail.

Use Bitcoin terms for the exact behavior established. Distinguish a transaction
rejected by policy from an invalid block, a disconnected peer from a stalled
node, and a failed wallet operation from lost funds. Name the actor, input or
event, changed check or state, and observable consequence. Replace phrases such
as "breaks validation" or "creates a DoS risk" with what the code actually does.
Preserve required options, costs, timing, recovery, and uncertainty when they
determine the claim. Do not add a stronger claim to make a title sound decisive.

State the scope and each qualification once, preserving relevant uncertainty
and any verifier-required production-evidence qualification in a concise
sentence. For a finding confined to fuzzing, tests, or tooling, name that scope
without adding a production-risk disclaimer unless the finding makes a
production-impact claim. When the conditional mechanism establishes the
finding, discuss corpus prevalence only if it affects the claim, consequence,
or severity.

Keep investigation history, exhaustive case lists, and repeated proof in the
verifier's decision reason and source evidence. The public explanation should
give the author enough evidence to assess and act on the finding. The judge
may only publish supplied claims with their qualifications and corrections.

For example, when source inspection establishes the missing coverage:

```json
{
  "title": "Taproot negative cases miss the push-length check",
  "summary": "Removing the second-byte check would still leave all new assertions passing.",
  "body": "Every negative case already fails either the script-length check or the first-byte check. Change the existing 34-byte negative case to start with OP_1. It would then pass those other checks and depend on the incorrect second byte being rejected."
}
```

This example describes coverage inferred from source. Do not claim that a test
ran unless execution evidence supports that claim.
