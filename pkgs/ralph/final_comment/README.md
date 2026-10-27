# Tune the review comment

Edit the Markdown files here to change the comment's layout. You can preview
changes without API keys, a Git checkout to review, or a running service:

```sh
python3 pkgs/ralph/final_comment/preview.py > /tmp/ralph-comment.md
```

Open `/tmp/ralph-comment.md` in your Markdown previewer. The default example has
findings, a design assessment, an alternative, prior discussion and a coverage
limitation. Check the failed-verification example too:

```sh
python3 pkgs/ralph/final_comment/preview.py \
  pkgs/ralph/final_comment/examples/verification-failed.json \
  > /tmp/ralph-comment-failed.md
```

These are fictional examples. To try different content, copy an example JSON
file and pass its path to the same command. Include a before/after preview in
your pull request so reviewers can see the layout change.

The shorter GitHub comment uses `github.md` and the same `finding.md`:

```sh
python3 pkgs/ralph/final_comment/preview.py --github > /tmp/ralph-github-comment.md
```

## Which file to edit

| File | Controls | Placeholders |
| --- | --- | --- |
| `comment.md` | Section order and any text shared by every comment | `verification_failed`, `backport`, `findings`, `design`, `alternatives`, `history`, `empty`, `limitations` |
| `finding.md` | Each finding's title, source location and body | `title`, `location`, `side`, `body` |
| `findings.md` | The findings heading | `findings` |
| `severity.md` | Each severity group's heading and findings | `heading`, `findings` |
| `design.md` | The design and concept section | `design` |
| `alternatives.md` | The alternatives section | `alternatives` |
| `backport.md` | The backport verdict and supporting details | `verdict`, `summary`, `details` |
| `history.md` | Earlier findings and author replies | `unresolved_notice`, `history` |
| `history_failed.md` | Earlier finding references after failed verification | `history` |
| `limitations.md` | The coverage notice and limitations | `coverage_notice`, `limitations` |
| `github.md` | The GitHub comment's section order and report link | `new_findings_notice`, `retained_notice`, `findings`, `report_url` |

Placeholders use `${name}` or `$name`. Keep each listed placeholder exactly
once in its file; the preview rejects missing, repeated or unknown placeholders.
Write `$$` for a literal dollar sign. The templates use Python's standard
`string.Template`; they have no loops or conditions.

To move limitations above findings, move `${limitations}` above `${findings}`
in `comment.md`. Keep a blank line between section placeholders. Ralph omits
empty sections and their blank paragraphs. The other files control headings
and the Markdown around their content.

Ralph still decides which findings were verified, which earlier reports refer
to the same concern, and when author responses belong in the history section.
The failure, incomplete-coverage and unresolved-history notices come from
Python and must stay in the templates. Editing the layout cannot remove their
placeholders. Failed verification retains earlier titles and statuses without
republishing their unverified bodies.

For the wording of findings and concept summaries, edit the
[collator prompt](../audits/collator.md). That prompt edits accepted
findings before these templates format them.

## Renderer changes

`__init__.py` holds the rendering decisions. `load_format()` reads and validates
the templates and returns a JSON-serializable snapshot. `validate_format(value)`
validates a saved snapshot and returns a copy. `render(...,
comment_format=snapshot)` uses that snapshot without rereading files. Saved
engine configurations include the snapshot so template edits change run
identity and replay keeps the original format.

`render_github(findings, *, head_sha, report_url, has_retained=False,
comment_format=None)` formats the shorter upstream comment. The service selects
publishable findings and retains ownership of comment markers, revision checks
and publication. `--github` previews only its visible text.

Run the relevant checks from the repository root:

```sh
PYTHONPATH=pkgs/ralph python3 -m unittest discover -s pkgs/ralph -p test_comment.py
PYTHONPATH=pkgs/ralph python3 -m unittest discover -s pkgs/ralph -p test_protocol.py
```
