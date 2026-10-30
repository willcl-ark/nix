Start from material production behavior changed by this PR, even if no tests
changed. Find the existing unit, functional, or fuzz coverage and check which
assertion would fail under a realistic regression. For a gap, name the smallest
useful test, its input or event sequence, and its observable failure. Only then
assess test reliability, duplication, fixtures, and runtime.

Keep this pass focused on regression evidence. Read production code to identify
what the assertions must distinguish, then inspect those assertions and their
initial state. Do not spend the pass on a general production review while the
assigned tests remain unread. A concrete bug found along the way may be reported,
but it does not complete the outstanding coverage assignments.

A follow-up pass may supply only remaining assignments, prior test assessments,
and already reported candidates. Inspect those remaining groups, reuse relevant
prior evidence, and avoid reporting an existing candidate again. Resolve each
supplied deferred question before marking its paths checked; inspecting another
behavior in the same file does not resolve it. A confirmed
missing assertion is a completed inspection: mark it checked and report its
specific limitation. Reserve deferred for work that remains uninspected.

Return `test_coverage` alongside the ordinary discovery fields. Group the
required test-review paths by the behavior they jointly implement or test.
Account for every required path, including production changes with unchanged
tests. For each group, give `paths`, `status`, `evidence`, `regression`, and
`reason`. Use `checked` only after inspecting the relevant implementation and
assertions: cite exact source excerpts, including unchanged tests when useful,
and name a plausible wrong implementation those assertions distinguish. A
confirmed missing assertion can also be checked and reported as a finding.
Use `deferred` with the specific unanswered inspection question when work is
unfinished. Use `not_applicable` with a concrete reason for a mechanical change,
or for generated data whose validation is explained in another group. Do not
call coverage complete because a fixture exists or a file excerpt was supplied.

For deployment or mode changes, compare the last old-state and first new-state
behavior, rollback or reorg across the transition, and the initial state used
by each test. Distinguish unconditional mempool policy from activated block
rules. Check whether defaults such as always-active regtest hide an incorrect
gate, bypass, or one-step delay. Inspect the actual functional assertions;
production-code inspection alone does not establish regression protection.

For changes confined to tests or test infrastructure, start from the changed
test or contributor workflow. A broken test runner or unreliable CI check is
worth reporting without a production-code defect.

Inspect whether changed tests provide useful, reliable evidence for the
intended behavior. Read assertion helpers and relevant production code when
their semantics matter.

For an important assertion, identify the regression it would catch and why.
Name the exact next value, event, or state transition the implementation should
produce. Check whether a realistic regression would make that assertion fail,
or whether the test would still pass after the behavior broke. Distinguish a
request or entry log from completed behavior, especially logging that happens
before a task finishes. Check that expected and forbidden events belong to the
observation window and that the asserted sequence matches the implementation's
state transitions.

Check that the test input and initial state distinguish the intended behavior
from a plausible incorrect implementation. Empty, default, or already-processed
data can make different behaviors produce the same result. For modes, aliases,
and defaults, consider a wrong mapping or ignored option. Identify the smallest
valid input and assertion that would expose it, reusing existing fixtures where
practical.

For a fix, identify which regression assertion would fail without the fix and
why. Check whether it distinguishes comparison-base behavior from head, accounting
for any test-harness changes needed to make the comparison meaningful.
Supplementary tests may protect adjacent behavior; name the regression each
would catch. For each functional test step, ask what must hold about
timing or ordering and whether a sanitizer, valgrind, or a loaded runner still
guarantees it: thin margins around sleeps or ensure_for, message order across
connections or nodes without a sync, state leaking between subtests through
the mempool, mocktime, restarts with different arguments, or reused ports,
unseeded randomness or hash-ordered containers deciding a branch, and
hard-coded ports or descriptor counts. Prefer setmocktime or bumpmocktime
where time is mockable and event-driven waits where it is not. Unit tests must
not leak SetMockTime or other globals between cases. Fuzz targets bound loops
with LIMITED_WHILE on ConsumeBool, avoid non-static globals and mutexes
because every target links into one binary, and assert outcomes rather than
merely reaching code.

Check whether tests contact real services, routers, or other resources outside
their controlled fixtures. Examine sleeps and repeated checks: what event are
they waiting for, what progress can occur during that interval, and what
regression would be exposed by the wait? Prefer an observable completion signal
when it establishes the necessary ordering. Keep a bounded observation period
when the behavior itself is "nothing else happens during this window"; otherwise
do not defend a wait unless it proves a useful ordering or timeout property.

Check whether each added test belongs in this suite, duplicates existing
coverage, or can reuse an existing fixture. Judge the marginal regression
coverage against the fixture setup, test runtime, and maintenance burden. Flag
tests that add cost without useful evidence, or suites whose setup is out of
proportion to the behavior they protect. Suggest deleting or consolidating
tests only when the useful regression coverage remains. Do not impose a test
count target. Report concrete deficiencies and useful simplifications, not
generic requests for more coverage.

When a changed default affects performance or reliability, inspect the evidence
for its value. Does a measurement use representative workloads and compare
meaningful alternatives, or does a test merely assert the chosen constant?
Distinguish regression coverage from evidence that the default suits users.
If important evidence is missing, name the unresolved tradeoff and the smallest
useful measurement; do not request a benchmark suite for a harmless choice.
