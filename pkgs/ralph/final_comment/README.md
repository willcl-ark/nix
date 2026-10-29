# Tune the review comment

Edit the Markdown files here to change the comment's layout. You can preview
changes without API keys, a Git checkout to review, or a running service:

```sh
python3 pkgs/ralph/final_comment/preview.py > /tmp/ralph-comment.md
```

Open `/tmp/ralph-comment.md` in your Markdown previewer. Comments show at most six
current verified findings. To inspect their full explanations, alternatives and
review history, preview the same example as a report:

```sh
python3 pkgs/ralph/final_comment/preview.py --report > /tmp/ralph-report.md
```

Check the failed-verification example too:

```sh
python3 pkgs/ralph/final_comment/preview.py \
  pkgs/ralph/final_comment/examples/verification-failed.json \
  > /tmp/ralph-comment-failed.md
```

These are fictional examples. To try different content, copy an example JSON
file and pass its path to the same command. Include a before/after preview in
your pull request so reviewers can see the layout change.

The GitHub comment uses `github.md` and the same `brief_finding.md`:

```sh
python3 pkgs/ralph/final_comment/preview.py --github > /tmp/ralph-github-comment.md
```

## Which file to edit

| File | Controls | Placeholders |
| --- | --- | --- |
| `comment.md` | Section order and any text shared by every comment | `verification_failed`, `overview`, `backport`, `findings`, `design`, `alternatives`, `history`, `empty`, `limitations` |
| `brief_finding.md` | Each comment finding's title, summary and source location | `severity`, `title`, `summary`, `location`, `side` |
| `finding.md` | Each report finding's visible summary and expandable explanation | `severity`, `title`, `summary`, `location`, `side`, `body` |
| `retained.md` | A compact earlier finding, including its explanation and provenance | `label`, `summary`, `location`, `side`, `body`, `provenance` |
| `provenance.md` | Earlier report references for a materially changed finding | `provenance` |
| `resolved.md` | Collapsed history of fixed or invalid findings | `history` |
| `findings.md` | The findings heading | `findings` |
| `design.md` | The design and concept section | `design` |
| `alternatives.md` | The alternatives section | `alternatives` |
| `backport.md` | The backport verdict and supporting details | `verdict`, `summary`, `details` |
| `history.md` | Earlier findings, author replies and resolved history | `label`, `history` |
| `history_failed.md` | Earlier finding references after failed verification | `history` |
| `limitations.md` | The coverage notice (`limitations` is empty; diagnostics are in the report) | `coverage_notice`, `limitations` |
| `github.md` | The GitHub comment's section order and report link | `new_findings_notice`, `retained_notice`, `findings`, `report_url` |

Placeholders use `${name}` or `$name`. Keep each listed placeholder exactly
once in its file; the preview rejects missing, repeated or unknown placeholders.
Write `$$` for a literal dollar sign. The templates use Python's standard
`string.Template`; they have no loops or conditions.

To move limitations above findings, move `${limitations}` above `${findings}`
in `comment.md`. Keep a blank line between section placeholders. Ralph omits
empty sections and their blank paragraphs. The other files control headings
and the Markdown around their content.

Comments show severity, title, a one-sentence summary and source location for at
most six current verified findings. They sort by severity, preserving the
verifier's impact and evidence order within each severity. Additional findings
link to the full report. Earlier unresolved concerns receive one short notice;
their entries, resolved history, finding bodies and alternatives stay in the
report. Concept feedback and a brief backport assessment can still appear.

Reports retain every finding and its expandable explanation. Previous findings
are grouped under one collapsed section, including their original review links,
revision history and author replies. Fixed and invalid findings remain under
"Resolved findings", with the resolution reason.

Ralph still decides which findings were verified and which earlier reports refer
to the same concern. The verifier's `unchanged_since_report` assessment controls
retained entries in the report, including regressions introduced by the PR.
Editing the wording alone does not count as a changed finding.
Dynamic HTML summary labels are escaped, with backtick code spans rendered
as HTML code tags. New findings require a summary; older
review records without one retain their stored explanation.
The failure, incomplete-coverage and overview notices come from
Python and must stay in the templates. Editing the layout cannot remove their
placeholders. Failed verification retains earlier titles and statuses in the
report without republishing their unverified bodies.

For the wording of findings and concept summaries, edit the
[collator prompt](../agents/collator/prompt.md). That prompt edits accepted
findings before these templates format them.

## Renderer changes

`__init__.py` holds the rendering decisions. `load_format()` reads and validates
the templates and returns a JSON-serializable snapshot. `validate_format(value)`
validates a saved snapshot and returns a copy. `render(...,
comment_format=snapshot)` uses that snapshot without rereading files. Saved
engine configurations include the snapshot so template edits change run
identity and replay keeps the original format.

`render()` produces the concise comment. `render_report()` preserves the full
review and is used for saved pipeline content. Both use the template snapshot;
the display limit does not truncate structured exports. `render()` also accepts
`report_url` for links to findings beyond the comment limit.

All renderers accept `review_tier` to retain the final selected routing tier in hidden HTML metadata
after the template content. When no tier is available, they omit the footer.

`render_github(findings, *, head_sha, report_url, has_retained=False,
comment_format=None, review_tier=None, repository_url=None, base_sha=None)` formats the shorter upstream comment.
The service selects publishable findings and retains ownership of comment
markers, revision checks and publication. `--github` previews only its visible text.

Run the relevant checks from the repository root:

```sh
PYTHONPATH=pkgs/ralph python3 -m unittest discover -s pkgs/ralph -p test_comment.py
PYTHONPATH=pkgs/ralph python3 -m unittest discover -s pkgs/ralph -p test_protocol.py
```
