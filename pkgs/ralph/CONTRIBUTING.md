# Contributing to Ralph

Start with the [agent flow](agents/README.md) for a short explanation of selection
and verification. Each agent has a `config.toml` that names its Markdown prompt.
Start with the agent you want to change. You do not need to edit the review runner to tune a
prompt, model, reasoning effort or tool limit.

| Change | File |
| --- | --- |
| General first-pass review | [independent/prompt.md](agents/independent/prompt.md) |
| Instructions shared by discovery agents | [shared/discovery.md](agents/shared/discovery.md) |
| A specialist's review | `<agent>/prompt.md`, such as [design/prompt.md](agents/design/prompt.md) |
| Which leads survive verification | [verifier/prompt.md](agents/verifier/prompt.md) |
| Public finding structure and writing guidance | [shared/finding-format.md](agents/shared/finding-format.md) |
| Wording of accepted findings | [collator/prompt.md](agents/collator/prompt.md) |
| Comment headings, order and finding layout | [final_comment](final_comment/README.md) |
| An agent's model, reasoning or limits | `<agent>/config.toml` |

From the repository root, check your changes with Python 3.11 or later:

```sh
python3 pkgs/ralph/review.py check-agents
python3 -m unittest discover -s pkgs/ralph -p 'test_*.py'
```

These commands need no credentials and make no model requests. The first checks
both profiles, every prompt and the comment templates. The tests check the
review workflow; they do not measure whether a new prompt produces better
reviews. In your PR, explain what behavior you want to improve and give an
example when you have one. Prompt wording does not need a matching text assertion.

PRs run these checks and save rendered comment previews automatically. A
maintainer can also approve a [live BASE/HEAD comparison](CI.md) on a Bitcoin PR
to see how a prompt, model or budget change affects a real review.

## Settings

The root of `config.toml` defines the default settings. A `[profiles.high]`
table overrides individual settings when the high profile is selected. Omit
that table when both profiles use the same settings. For example:

```toml
prompt = "prompt.md"
tool_set = "focused"
model = "gpt-6-luna"
reasoning_effort = "low"
max_output_tokens = 4000
max_tool_calls = { routine = 12, standard = 24, sensitive = 48 }

[profiles.high]
model = "gpt-6.1-sol"
reasoning_effort = "high"
max_output_tokens = 16000
```

The existing files also spell out discussion, history and web-search allowances
and request timeouts. `max_tool_calls` must name all three review tiers. Profile
overrides replace a whole setting, including that table. Output tokens are per
response; timeouts are in seconds. The web-search allowance is a reservation
estimate because the provider may return more searches than requested.

The supported tool sets are:

| Tool set | Access requested |
| --- | --- |
| `none` | No tools, used by the feedback mapper and collator |
| `focused` | Find paths, read files and diffs, search current code |
| `full` | Code, comparison-base history and other discussion lookup |
| `base` | Find, read and search files at the comparison base |
| `research` | Full tools plus current discussion and hosted web search |

The runtime still applies evidence availability, operator web permission and
stage restrictions. Blind alternatives can only inspect the comparison base.
Source tools read immutable repository snapshots. Only `write_verifier` receives
experiment commands for an isolated disposable copy; it shares the review
spending ceiling and preserves final-verifier headroom. A new
tool implementation or a new pipeline stage requires Python changes. Model IDs
must have accounting rates in [spend.py](ralph/spend.py).

[profiles.toml](agents/profiles.toml) sets the shared review budget multiplier for each
profile. Per-run `engineConfig.stages` overrides apply after agent settings.
Resolved prompts, settings and comment templates are saved with each review.
Frozen evaluations keep those saved values until explicitly given new settings.

## Shared prompts and special cases

- [shared/finding-format.md](agents/shared/finding-format.md) is automatically
  appended to verifier and collator prompts when loading an agent directory.
  Saved reviews retain the loaded text for replay.
- Both adversarial reviewers use [adversarial/prompt.md](agents/adversarial/prompt.md).
  Claude's config points to that file; its model and limits remain independent.
  Domain instructions live in [adversarial/domains](agents/adversarial/domains).
- [shared/backport.md](agents/shared/backport.md) supplements stages reviewing backports.
- [verifier/reconciliation.md](agents/verifier/reconciliation.md) covers earlier findings.
- [feedback](agents/feedback) maps earlier findings to author replies before verification.
- The service's later addressed-finding checks remain in the companion package's
  [follow-up prompt](../ralph-service/audits/addressed_findings.md).

To try a complete copied agent directory, pass `--agents-dir DIR` to
`check-agents` or `review`. The service uses `services.ralph.agentsDir` or
`engineConfig.agents_dir`. This replaces the old separate prompt-file,
audit-prompt-directory and model-map overrides.
