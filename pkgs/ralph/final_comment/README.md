# Tune review presentation

The Review report and PR comments use the same approved findings. The judge
writes their title, summary and explanation once. Renderers select and format
that wording without another model call. The service's Run archive holds
intermediate analysis, alternatives, detailed history and execution diagnostics.

Preview the built-in examples without API keys or a running service:

```sh
python3 pkgs/ralph/final_comment/preview.py > /tmp/ralph-comment.md
python3 pkgs/ralph/final_comment/preview.py --report > /tmp/ralph-report.md
python3 pkgs/ralph/final_comment/preview.py --github > /tmp/ralph-github-comment.md
python3 pkgs/ralph/final_comment/preview.py --report \
  pkgs/ralph/final_comment/examples/verification-failed.json \
  > /tmp/ralph-report-failed.md
```

These examples are fictional. Copy an example JSON file and pass its path to
preview different content. Include before/after previews with layout changes.

## Templates

| File | Purpose | Placeholders |
| --- | --- | --- |
| `comment.md` | Shared report/comment section order | `verification_failed`, `overview`, `backport`, `findings`, `design`, `empty`, `limitations` |
| `brief_finding.md` | PR comment finding | `severity`, `title`, `summary`, `location`, `side` |
| `finding.md` | Report finding and expandable explanation | `severity`, `title`, `summary`, `location`, `side`, `body` |
| `findings.md` | Findings heading | `findings` |
| `design.md` | Approved concept objection | `design` |
| `backport.md` | Backport conclusion and supporting details | `verdict`, `summary`, `details` |
| `limitations.md` | Coverage notice | `coverage_notice` |
| `github.md` | Upstream PR comment and report link | `new_findings_notice`, `retained_notice`, `findings`, `report_url` |

Each placeholder must appear exactly once in its template. Use `$$` for a
literal dollar sign. Templates use Python's `string.Template`, with no loops or
conditions. Empty sections disappear along with their blank paragraphs.
Verification and coverage notices cannot be removed by changing templates.

## Content and ordering

PR comments show at most six findings, each with its approved summary and source
location. The Review report includes every current verified finding with its
explanation, plus approved concept/backport conclusions and a coverage notice.
The service renders approved structured fields with the same renderer. Detailed
coverage logs stay in the Run archive. Web reports link to its coverage section;
CLI previews retain a brief notice. Failed verification remains explicit.
Both use the same severity ordering and preserve the supplied impact order on
ties. Additional comment findings link to the report.

Historical concerns join the current findings only after the pipeline verifies
the current claim and the judge approves publication. Reconciliation alone
cannot publish a finding, and the renderer never adds findings from history.
Unconfirmed history gets a brief notice; its original claims, author replies
and resolved history belong in the Run archive. Failed verification suppresses
finding bodies and retains explicit failure and incomplete-coverage notices.

Change the wording instructions in the [judge prompt](../agents/judge/prompt.md).
The judge decides publication and approves the exact wording. Keep explanations
focused on evidence and qualifications that add to the summary.

## Renderer interfaces

`load_format()` validates the templates and returns a JSON-serializable snapshot.
`validate_format(value)` checks a snapshot and returns a copy. Saved engine
configurations include that snapshot, so template edits change run identity and
replay uses the saved format. The report/archive cutover removes obsolete
history and alternatives templates. Publication retries using the old saved
template schema may fail and require a newly requested review. There is no
automatic replay, migration or fallback. Completed runs retain their saved text in the canonical JSON; human pages use
the approved structured fields and current layout.

`render()` produces the bounded PR comment. `render_report()` produces the
human review stored in pipeline content. `render_github()` formats findings the
service has selected for upstream publication. The service owns publication
eligibility, markers and revision checks. `review_tier` is retained as hidden
HTML metadata by each renderer when supplied.

Run the focused checks from the repository root:

```sh
PYTHONPATH=pkgs/ralph python3 -m unittest discover -s pkgs/ralph/tests -p test_comment.py
PYTHONPATH=pkgs/ralph python3 -m unittest discover -s pkgs/ralph/tests -p test_protocol.py
```
