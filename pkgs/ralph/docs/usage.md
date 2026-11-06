# Running a local review

See the [package README](../README.md) for a first invocation. This guide covers
revision selection, provider configuration and optional review inputs for the
bundled `current` engine. Use `ralph review --help` for the complete CLI options.

## Revisions and output

Branch mode requires an explicit `--base` and compares the unique merge base
with the target. An explicit `BASE..HEAD` range compares those exact committed
trees, including diverged endpoints. Both endpoints are required. Working-tree
and index changes, root commits, and triple-dot shorthand are unsupported.
Ralph resolves revisions once, pins their objects until the run finishes, and
reads blobs without checkout or index changes. It never fetches Git objects or
calls a forge. Missing objects or history fail before model requests.

Stdout contains review Markdown. Progress and errors go to stderr. `--output`
writes a private versioned JSON artifact containing engine identity, base/head,
merge base, actual diff base, native data, supported exports, usage and status.
Use `--help` for tool limits and trusted local engine options.

## Provider configuration and request limits

The current engine sends Codex and Claude requests through the same
OpenAI-compatible endpoint and access key. Configure CLIProxyAPI with both
accounts and put its URL in an engine configuration:

```json
{"openai_api_base": "http://127.0.0.1:8317/v1"}
```

Pass this file with `--engine-config` and use the proxy's access-key file with
`--openai-key-file`. Ralph still supplies the prompts and executes tools; no
coding CLI is launched. The URL is recorded in engine identity, so keep
credentials out of it. Review stages and audits use this URL without falling
back to the OpenAI endpoint on failure. Claude strict JSON schemas become
prompt guidance at the proxy; Ralph parses and validates returned results.

Ralph records request attempts and provider-reported token usage without pricing
it or using monetary admission limits. Missing usage remains unknown.
CLIProxyAPI removes `max_output_tokens` before forwarding Codex requests, so the
requested output allowance is not an enforced cap on that path. Ralph omits
`max_tool_calls` from outgoing requests because Codex rejects it, and enforces
its own tool limits. Hosted search counts are recorded even when a response
exceeds the configured allowance. The proxy/provider enforces subscription
limits.

Model requests retry HTTP 429, 5xx and transient transport failures up to three
total attempts, preserving the current conversation. Backoff is one then two
seconds; a numeric `Retry-After` can extend either wait up to ten seconds.
Other HTTP errors, including rejected parameters, fail immediately. Every
attempt checks cancellation and records a separate usage attempt. Missing
usage after a timeout or server error is not estimated.
Failed attempts remain in diagnostics even when a retry succeeds. HTTP failures
retain the status and a bounded, redacted provider message, without the raw
error response or request body.

## Context and research

Archaeology defaults off for local changes. `--context FILE` can supply versioned
review context and optional source metadata, for example:

```json
{
  "schema_version": 1,
  "title": "Review title",
  "rationale": "Problem and intended change",
  "source": {
    "kind": "pull_request",
    "url": "https://git.example.org/owner/repo/pulls/123",
    "number": 123
  }
}
```

The `source` field is optional. A PR source records its actual URL and number.
`--research-evidence FILE` supplies captured discussion evidence. `--archaeology auto|on|off` resolves to
a boolean before engine identity is recorded. Forced enabling requires usable
PR context and discussion evidence. Disabled archaeology and blind alternatives
make no model requests and do not reduce ordinary code coverage. Hosted web
permission is separate from discussion access.

See [concept and history](engine.md#concept-and-history) for how this evidence
is used. The local CLI never fetches discussions itself.

## Profiles and overrides

The default profile uses the checked-in agent settings. Select high
explicitly with `ralph review ... --profile high` or `"profile": "high"` in the
engine configuration. Consensus-domain routing and conservative fallback also
promote remaining stages to high automatically. The effective profile and reason
are recorded. Classification follows changed behavior; a consensus filename
alone does not cause promotion.

Agent [configuration files](../agents/) define the models, reasoning effort,
output allowances and tool limits for each profile. High increases selected
stage allowances; it is not a guarantee of better review. To edit these defaults
or supply a complete `--agents-dir` tree, see [Contributing](../CONTRIBUTING.md).

A resolved profile records every stage's model, reasoning effort, output ceiling,
and tool limits in the engine configuration snapshot and hash. Profiles keep
routine, standard and sensitive tool limits explicit. Each agent owns its model
and limits, including alternatives and feedback. A high-profile table overrides
that agent's defaults; explicit stage overrides apply last.

An engine configuration can override individual settings on any existing stage
through `stages`. These partial overrides apply after the selected agent profile.
For example, to reduce verification reasoning to high:

```json
{
  "openai_api_base": "http://127.0.0.1:8317/v1",
  "stages": {
    "verifier": {
      "model": "claude-opus-5-5",
      "reasoning_effort": "high",
      "max_output_tokens": 64000
    }
  }
}
```

Pass this file with `--engine-config`. Unspecified settings retain their
resolved defaults. Supported settings also include `timeout`, `final_timeout`,
`max_tool_calls`, `max_context_calls`, `max_history_calls`,
`max_web_search_calls_per_response` and `tool_set`. `max_tool_calls` names all three tiers,
`routine`, `standard` and `sensitive`. Unknown stages and fields are rejected;
model IDs must be nonempty. Ralph records the resolved settings in
the profile snapshot and hash, including timeouts, for retries and evaluation.
Explicit timeout overrides and saved jobs retain their configured values.
All stages use the same endpoint and key, with their own recorded usage.

The companion's follow-up prompt and model settings are configured separately.
Main-review overrides do not change those assessments.

## Optional reproduction experiments

The write-verifier can investigate supported Major findings before judging.

Experiments use the pinned Bix development environment supplied by the Nix
package. Each `experiment_run` starts with pristine head or comparison-base
source in a bounded temporary filesystem. Include every edit, build and test in
one script; a later call does not retain earlier edits or build products. The
runner records the revision, command, exit status, bounded output, and source
patch including new tests. Commands execute without host credentials, service
state or outbound network; functional tests have private loopback. Failure to
start the sandbox leaves the finding eligible for static verification.

The NixOS module provisions a delegated cgroup for experiments. Each experiment
has an 8 GiB total memory limit including temporary files, no swap, 128 tasks,
and four CPUs of aggregate execution time per wall-clock interval. A ccache
directory persists across experiments when the service configures one. Experiments
share a 30-minute stage deadline with model requests, and service workers
serialize experiments. Commands should build under `/tmp/build` so recorded
source patches are not filled with compiler output.

For a standalone CLI run, start a delegated user service and identify its
subtree before invoking Ralph:

```sh
systemd-run --user --collect --wait --pipe \
  -p 'Delegate=cpu memory pids' -p DelegateSubgroup=controller \
  bash -c '
    group=$(sed -n "s/^0:://p" /proc/self/cgroup)
    export RALPH_WRITE_VERIFIER_CGROUP="/sys/fs/cgroup${group%/controller}"
    exec ralph "$@"
  ' ralph review topic /path/to/repo --base origin/master \
  --engine-config /path/to/engine.json \
  --openai-key-file /path/to/proxy-key
```

Without a delegated subtree or the packaged experiment environment, execution
is unavailable and the final judge still considers source evidence.

For service provisioning, see the [NixOS module](../../../modules/ralph/README.md).
Experiment outcomes and publication rules are described in the
[engine reference](engine.md#findings-and-publication).
