# Checking Ralph changes in CI

Pull requests that change Ralph run configuration validation, the core and service
tests, and comment previews. Download `ralph-comment-previews` from the workflow
run to inspect the rendered examples. These checks use no model credentials.

## Running a live comparison

A maintainer can run **Ralph live comparison** from the repository's Actions tab.
The workflow must first be merged into the default branch. Select that branch
when starting a run and supply:

- The open Ralph PR number.
- Its full HEAD commit SHA, after inspecting that revision.
- A public `bitcoin/bitcoin` PR number to review.
- The CLIProxyAPI HTTPS URL reachable through Tailscale, ending in `/v1`.

Starting the workflow approves two live reviews. It rejects a closed PR or a HEAD
that no longer matches the approved SHA. If the PR changes afterward, the run
still uses the approved commit. Start a new comparison to evaluate a newer one.

The workflow fetches the Bitcoin PR from GitHub and freezes its code, metadata
and available discussion once. Open and merged Bitcoin PRs are supported; capture
pins the base commit recorded by GitHub so a merged PR retains its original diff.
Both reviews use that same case. BASE takes the
agent and template files from the merge base of the Ralph PR and the trusted
runtime commit. HEAD takes those files from the approved PR commit. Both use
Ralph's default profile and the same executable code from the default branch at
dispatch time. Python, Nix and workflow changes in the PR are not live-tested.

Only regular Markdown and TOML files under `agents/` and the Markdown comment
templates are exported. Candidate Python is never imported. Review tools read
the frozen repository and discussion; replay disables live discussion lookup
and hosted web search. This keeps the comparison repeatable, though model output
can still vary between runs.

The Actions summary compares findings, coverage, elapsed time, model/tool calls
and reported token usage. Expand **BASE review** and **HEAD review** to read the
rendered comments and their limitations. Missing metrics remain unavailable;
token totals with missing usage are marked as partial. Very large reviews stay
in the artifact to keep the summary within GitHub's size limit.

Download `ralph-comparison-<run>-<attempt>` for `base/review.md`, `head/review.md`,
the review JSON artifacts and `summary.json`. They record the revisions, resolved
settings, findings, status, usage and timing. A failure on one side does not skip
the other. Findings and wording differences need human review; finding counts do
not measure accuracy. CI does not post comments to either PR.

## Maintainer setup

Create these repository secrets:

| Secret | Value |
| --- | --- |
| `CLI_PROXY_API_KEY` | A separate inference client key accepted by CLIProxyAPI |
| `TAILSCALE_AUTH_KEY` | A reusable, ephemeral, pre-approved auth key for the CI tag |

Give the CI tag access only to the proxy host and port in the tailnet policy.
Check existing grants too: Tailscale grants add access, so a broader grant can
also permit connections. Use an inference client key, not the proxy's management
key or provider credentials. The action logs the runner out when the job ends.

Inspect model, reasoning, tool and timeout changes before starting a run. Each
rerun starts a fresh pair of reviews and records their reported usage. Subscription
quotas are enforced by the proxy and provider.

The credentialed workflow uses `workflow_dispatch` and runs only from the default
branch. The automatic checks use `pull_request`. Candidate files are read as data
by trusted code before CI joins Tailscale or receives the inference key. Artifacts
contain results only, and the temporary key file is deleted after replay.
