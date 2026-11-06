# Ralph local review

Ralph reviews committed Bitcoin Core changes. Its default `current` engine
uses independent discovery passes, challenges their findings, verifies source
evidence, and sends supported findings to a final judge. Optional isolated
experiments can investigate Major findings before the judge decides publication
and writes the final wording.

The `ralph` package installs the local review command. The
[companion service](../ralph-service/README.md) handles webhooks, publication,
reports, statistics and follow-up reviews. See the
[NixOS module](../../modules/ralph/README.md) to deploy it.

## Run a review

Build the package from this flake:

```sh
nix build .#ralph
```

The current engine needs an OpenAI-compatible endpoint serving its configured
Codex and Claude models. Configure CLIProxyAPI with both accounts and save an
engine configuration such as:

```json
{"openai_api_base": "http://127.0.0.1:8317/v1"}
```

Use the proxy's access-key file and a local repository containing the commits:

```sh
./result/bin/ralph review topic /path/to/repo \
  --base refs/remotes/origin/master \
  --engine-config /path/to/engine.json \
  --openai-key-file /path/to/proxy-key \
  --output /tmp/topic-review.json
```

Branch mode compares the unique merge base with the target. To compare two
exact committed trees, replace `topic` and `--base ...` with a range such as
`'HEAD^..HEAD'`.

Ralph reads pinned Git objects without changing the checkout or index. It never
fetches objects or contacts a forge. Missing objects or history fail before
model requests. Working-tree changes, root commits and triple-dot shorthand
are unsupported.

## Results and limitations

Stdout contains review Markdown; progress and errors go to stderr. `--output`
writes a private JSON artifact with engine identity, revisions, structured
results, usage and status. Keep credentials out of the endpoint URL because it
is recorded in engine identity.

Published findings require source excerpts checked against the reviewed Git
objects. Matching excerpts do not prove a claim is correct. Reviewers can miss
bugs, and an empty findings list is not an approval. Reports identify unread
code, failed stages and unresolved candidates. Failed source verification or
judging marks the run failed, and the CLI prints the report and exits with
status 1.

Reviews are primarily static. An unavailable or inconclusive reproduction
experiment does not invalidate otherwise supported findings. Optional history
research does not stop the selected code-review stages.

Requests record provider-reported token usage without monetary admission
limits. Tool limits are enforced locally; some provider output and search
allowances are advisory. See [usage and limits](docs/usage.md#provider-configuration-and-request-limits)
before running a review.

## Guides

| Task | Guide |
| --- | --- |
| Configure revisions, context, profiles or experiments | [Local usage](docs/usage.md) |
| Understand routing and the review pipeline | [Agent flow](agents/README.md) |
| Understand engine exports, evidence rules, failures or review history | [Engine reference](docs/engine.md) |
| Change prompts, models or tool limits | [Contributing](CONTRIBUTING.md) |
| Change comment or report layout | [Rendering guide](final_comment/README.md) |
| Check a change with a live comparison | [CI guide](CI.md) |
| Capture and replay a fixed review case | [Frozen evaluation](../../modules/ralph/evaluation.md) |
| Operate the webhook service | [Companion guide](../ralph-service/README.md) |

Prompt and template checks need no credentials or model requests. Start with
[Contributing](CONTRIBUTING.md) for commands and the files to edit.
