Find code introduced or made dead by this PR that can safely be removed.
Include unused functions, types, fields, constants, imports, unreachable
branches, dead stores, and obsolete test helpers or build wiring. Stay within
the consequences of this PR; do not inventory unrelated pre-existing dead code
or propose general design simplifications.

Start with the complete changed-path manifest and diff, including deletions.
Track both newly added definitions and the definitions, state, and dependencies
used by removed or replaced code. Follow those references into unchanged files:
removing the last caller can orphan a helper and everything used only by it.
Compare base and head to establish which change made each candidate dead.
Read complete relevant functions and guards; a hunk alone can hide a use or
the condition that makes a branch reachable.

Use `search_code` and `search_base_code` for distinctive literal symbol names,
then read the definitions and references. These searches are literal, not
regular expressions. Search across the repository rather than only changed
files. Use path lookup and base/head file reads to inspect build targets,
registration tables, and relevant callers. Prioritize plausible candidates
from the diff and stop pursuing a candidate when a valid use disproves it.
An empty search is a lead, not proof; truncated search results cannot establish
absence. Narrow an ambiguous query and record any decisive evidence the tools
cannot retrieve.

Distinguish executable uses from declarations, comments, and references inside
other dead code. Check callbacks and function pointers, virtual dispatch,
macros, templates, generated code, string-based registration, and test discovery
when relevant. Check supported platforms, optional build configurations, and
exported API contracts before declaring code unused. A use in tests, benchmarks,
or fuzzing is a real use; absence from production callers alone does not make
code dead. Do not invent external consumers for internal helpers, but do not
treat a documented external entry point as dead solely because this repository
has no caller.

For an unreachable branch, establish the controlling invariant and check all
relevant callers. For an unread value or dead store, check evaluation side
effects, object lifetime and destructor behavior, synchronization, and required
validation before recommending removal. An unused result does not make the
operation producing it removable. Preserve intentional security checks and
protocol-reserved behavior unless evidence establishes that removal is sound.

Report supported removals as kind `suggestion`, unless a separate correctness
failure is established. Identify what to delete, the PR change responsible,
the searches and caller/build evidence checked, and why removal preserves
required behavior. A verified unused helper is a concrete maintenance cost;
it does not need a runtime failure to justify removal. Group code orphaned by
the same change into one actionable finding. Anchor it to the responsible
changed source line, using the base side for a removed caller, and name any
unchanged dead definitions in the body. Use source line numbers, not diff line
numbers. Return no finding when reachability or safe removal remains speculative.
