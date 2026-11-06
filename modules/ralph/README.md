# ralph

This NixOS module runs `ralph-service`, which receives Forgejo pull request
webhooks, schedules reviews and publishes their results. It defaults to
`packages.<system>.ralph-service`; package overrides must provide the companion
service, not the standalone `ralph` command.

For local reviews, see the [package guide](../../pkgs/ralph/README.md).
The [agent guide](../../pkgs/ralph/agents/README.md) explains how reviews work;
[service operations](../../pkgs/ralph-service/README.md) covers jobs, recovery
and publication.

## Minimal configuration

```nix
{
  imports = [ inputs.will-nix.nixosModules.ralph ];
  services.ralph = {
    enable = true;
    openaiApiBase = "http://127.0.0.1:8317/v1";
    openaiKeyFile = "/run/secrets/ralph/proxy-key";
    repositories.core = {
      origin = "https://git.example.org/owner/repo.git";
      repository = "owner/repo";
      forgejoApi = "https://git.example.org/api/v1/repos/owner/repo";
      botLogin = "ralph";
      webhookSecretFile = "/run/secrets/ralph/webhook-secret";
      forgejoTokenFile = "/run/secrets/ralph/forgejo-token";
      workers = 3;
    };
  };
}
```

Set the Forgejo webhook to `POST` JSON to
`https://YOUR_HOST/webhooks/forgejo`. Configure a long random secret in Forgejo
and store the same value in `webhookSecretFile`. Select custom pull request
events.

Repository IDs such as `core` and `gui` are persistent identities. Do not rename
or reuse them for another source. One coordinator, queue and Telegram consumer
serve all repositories. Global options configure the account, directories,
listener, provider and scheduler; each repository selects its source,
credentials, strategy, worker allowance and publication policy.

The listener defaults to `127.0.0.1:8765`. Configure a reverse proxy for the
webhook endpoint. GitHub publication is disabled unless explicitly enabled.
Before reusing an older state directory, follow the service's
[state migration instructions](../../pkgs/ralph-service/README.md#uncertain-publication-recovery).

## Subscription inference through CLIProxyAPI

Import `will-nix.nixosModules.cli-proxy-api` alongside the Ralph module and
provision the proxy's runtime configuration and Codex and Claude logins. See
the [CLIProxyAPI module](../cli-proxy-api/README.md). Then configure Ralph:

```nix
services.cli-proxy-api = {
  enable = true;
  configFile = "/run/secrets/cli-proxy-api.yaml";
};
services.ralph = {
  openaiApiBase = "http://127.0.0.1:8317/v1";
  openaiKeyFile = "/run/secrets/ralph/proxy-key";
};
```

The key file contains the proxy's client access key, not the subscription OAuth
token. All review stages, including Claude adversarial review, and follow-up
assessments use the proxy and the same key. Ralph retains its prompts and tool
execution. A failed proxy request does not fall back to the paid OpenAI API.
The proxy converts Claude's strict JSON schema to prompt guidance; Ralph still
parses and validates the response before accepting it.

Stop new review intake and let active work finish before changing the endpoint
and key together. Queued jobs and frozen evaluations retain their recorded
engine configuration; requeue or recapture them for the new endpoint. Verify
the proxy's authenticated model list contains the configured Codex model IDs
and `claude-opus-5-5`, then run one local review with the proxy URL in `--engine-config` and inspect
its result before resuming the service queue. The local command does not publish
review comments.

CLIProxyAPI strips Codex's `max_output_tokens`, so the requested output cap
is not enforced on that path. Subscription limits are enforced upstream.
Ralph records returned usage without estimating charges or remaining quota.

## Options

Required deployment options:

- `services.ralph.repositories.core.origin`: Git remote URL used for `git fetch` and
  stale-head checks.
- `services.ralph.repositories.core.repository`: Forgejo repository full name, for
  example `owner/repo`.
- `services.ralph.repositories.core.forgejoApi`: Forgejo repository API URL ending in
  `/api/v1/repos/owner/repo`.
- `services.ralph.openaiKeyFile`: file containing the OpenAI API
  key or the configured proxy's client access key.
- `services.ralph.repositories.core.webhookSecretFile`: file containing the Forgejo
  webhook secret.
- `services.ralph.repositories.core.forgejoTokenFile`: file containing the Forgejo API
  token.
- `services.ralph.repositories.core.botLogin`: Forgejo login that owns the review
  comment.

Useful defaults:

- `services.ralph.listenAddress = "127.0.0.1"`
- `services.ralph.port = 8765`
- `services.ralph.stateDir = "/var/lib/ralph"`
- `services.ralph.repositories.core.commentMarker = null`, which uses
  `<!-- ralph:${repository} -->`
- `services.ralph.repositories.core.agentsDir = null`
- `services.ralph.followupPromptFile = null`
- `services.ralph.followupModel = null`

Set `agentsDir` to use a complete alternate agents directory. See
[Contributing](../../pkgs/ralph/CONTRIBUTING.md) for prompt, model and template
changes and their checks. Follow-up has separate `followupPromptFile` and
`followupModel` options.

Set `repositoryUrl` only when the HTML URL in Forgejo webhook payloads cannot
be derived from `forgejoApi`. See [the module source](nixos/default.nix) for the
full option definitions.

## Review engine

- `services.ralph.repositories.core.engine = "current"`: select the current review pipeline.
  Other names load trusted Python engine modules included in the selected
  package. Loading an engine runs its code with the service's permissions, so
  only use package code you trust.
- `services.ralph.repositories.core.engineConfig = { }`: JSON-serializable object passed to the
  selected engine. Ralph writes this configuration to the Nix store and pins
  its resolved settings when queuing each run. Do not put secrets in it.
  The current engine accepts resolved `prompts`, or `agents_dir`, plus
  `profile`, `routing_mode`, `archaeology`, `blind_alternatives`,
  `openai_api_base` and `stages`.
  The service enables archaeology for PR context with discussion access.

The current-engine options `agentsDir` and `routingMode` provide convenience
defaults for the current engine. Values in `engineConfig` take precedence over
those defaults. These convenience defaults apply only to the current engine.
Engine-produced Markdown is the published review body. Structured data stays
private unless an engine explicitly provides a supported export.

`services.ralph.openaiApiBase` defaults to `https://api.openai.com/v1`.
It sets the current engine's OpenAI-compatible Responses endpoint and the
follow-up endpoint. An explicit `engineConfig.openai_api_base` takes precedence
for both. Credentials belong only in `openaiKeyFile`, never in the URL.

Use `engineConfig.stages` to override individual settings on any existing
review stage. Overrides apply after the agent's settings for the selected
`default` or `high` profile. Unspecified settings keep their resolved defaults.
For example, to override verification reasoning:

```nix
services.ralph.repositories.core.engineConfig.stages.verifier = {
  reasoning_effort = "high";
};
```

See the [usage guide](../../pkgs/ralph/docs/usage.md) for supported stage
overrides, timeouts and profile precedence. Jobs retain resolved settings across
retries and restarts; saved results retry publication without rerunning models.
Do not put secrets in engine configuration or endpoint URLs.

`routingMode` accepts `"enabled"` for selective routing, `"shadow"` to record a
route while requesting every audit, and `"full"` to request every audit without
calling the router. `agentsDir` selects a complete alternative directory of
prompts and settings. These options belong under each repository.

## Experiment environment

A GUI repository can set `strategy = "gui"` and
`submodules.bitcoin = "https://github.com/bitcoin/bitcoin"`. Only explicitly
configured submodules are prepared, at the exact commits pinned by the GUI base
and head. Mirror synchronization needs no submodule commands. Set its
`writeVerifierEnvironment` to `packages.<system>.ralph-gui-verifier-env` for Qt
experiments. Other repositories have no submodules by default.

The flake packages include a build environment taken from the pinned
[Bix default devShell](https://github.com/bitcoin-dev-tools/bix). Dependencies
are realized when building the package, with no Nix daemon or network needed
inside an experiment. The environment includes Bix's compiler, libraries,
Python test dependencies and shell hooks. Bix follows this flake's nixpkgs.
Override `services.ralph.writeVerifierEnvironment` with a package whose main
program sets up the environment and executes its command arguments when
reviewing projects with different build dependencies.

Experiments use Bubblewrap with private mount, user, PID, network, IPC and UTS
namespaces. The review service allows these namespaces; hosts must allow
unprivileged user namespaces. A host that blocks them records experiments as
inconclusive. The experiment sees the Nix store read-only, its source copy,
temporary files, private loopback and a compiler cache under `stateDir/ccache`
that experiments share read-write, without Ralph's secrets or other state.
The service delegates CPU, memory and process controllers to the runner.
Each experiment has an 8 GiB total memory limit, no swap, at most 128 processes
and a CPU quota of four cores. The review controller runs in a separate subgroup
so an experiment exceeding its allowance cannot kill the controller through
that limit. The runner also bounds temporary storage and serializes experiments.
Without writable cgroup delegation, experiments are unavailable and findings
continue to the final source-based check.

## Public reports and statistics

For full browser reports, set `services.ralph.reportDir` to a
separate public directory and `services.ralph.reportBaseUrl` to
the HTTP URL serving it. The module creates the directory with mode `0755`;
ralph writes readable static HTML and JSON files before publishing a link
in the review comment. Configure your web server to serve this directory
without directory browsing. Do not serve the private `stateDir` or its
`review-traces` directory.

Serve directory indexes and redirect PR history paths to their trailing-slash
form. History and stats pages require same-origin scripts and
`connect-src 'self'`; history also requires same-origin form submissions. Keep
the stricter report policy on individual report HTML files.

With reports enabled, `ralph-stats.timer` refreshes `reportDir/stats/` and review
history every five minutes. `ralph-followup.timer` checks whether later PR
updates address earlier findings, using its own checkout and usage ledger under
`stateDir/followup/`. Its first run establishes a baseline rather than backfilling
old PR updates. See [reports and statistics](../../pkgs/ralph-service/operations.md#reports-and-statistics)
for interpretation, backfill and cache recovery.

To refresh statistics immediately:

```sh
systemctl start ralph-stats.service
```

### Subscription quotas

To add Codex and Claude subscription quotas from CLIProxyAPI v8:

```nix
services.ralph.subscriptionUsage = {
  enable = true;
  managementUrl = "http://127.0.0.1:8317";
  managementKeyFile = "/run/secrets/cli-proxy-management-key";
};
```

Set a separate nonempty `management.secret-key` in CLIProxyAPI. Inference client
keys cannot authenticate management requests. Keep management bound to loopback
with remote management disabled. The bundled `management.html` UI is optional;
quota polling works with the control panel disabled. No separate frontend is
needed. The stats service receives the management key through a systemd
credential; the review daemon and Telegram monitor do not receive it.

Stats polls upstream usage through the management API every five minutes,
without running inference or draining the proxy's temporary usage queue. Public
snapshots contain provider, plan when available, separate quota windows, reset
times and optional credit balances. Account identities, OAuth tokens, credential
paths and management keys are excluded. Failures are shown as unavailable, never
as unused capacity. Snapshot time is visible. Provider credit balances are not
converted into dollars, and subscription fees are not supplied by these endpoints.

## Review status images

The HTTP listener serves `GET /assets/<owner>/<repo>/<pr>.png` for the
configured repository. It returns a transparent 16 by 16 PNG dot:

- Grey when there is no published actionable feedback for the latest queued
  generation, including absent, pending, failed, skipped, partial or empty
  reviews without findings or a conceptual concern.
- Bitcoin orange (`#f7931a`) when the published review has verified findings
  or a conceptual concern.

Orange still applies when published feedback comes from a partial review.
Neither colour indicates approval or a pass/fail verdict.

A new queued generation resets the dot to grey until publication completes.
Existing saved results work immediately; no model calls or backfill are needed.

Forward `/assets/` to Ralph's listener alongside `/webhooks/forgejo`, preserving
the full path. This endpoint works independently of `reportDir`; the proxy
does not need access to private state. Responses use `Cache-Control: no-store`,
though external image proxies may apply their own caching.

For example, with `repository = "bitcoin/bitcoin"` and a host proxying the
listener at `ralph.example.org`:

```html
<img src="https://ralph.example.org/assets/bitcoin/bitcoin/123456.png"
     width="16" height="16" alt="Ralph review status">
```

## GitHub comments

Set the publication mode independently for each repository:

```nix
services.ralph.repositories.core.githubPublication = "off";
services.ralph.repositories.gui.githubPublication = "full";
```

`githubPublication` accepts `"off"`, `"major-only"` or `"full"` and defaults to
`"off"`. Configure
`githubRepository` with the upstream `owner/repo` and `githubTokenFile` with a
runtime secret file. The upstream and mirror must use matching PR numbers.
Public reports must also be enabled through `reportDir` and `reportBaseUrl`.

Modes behave as follows:

- `"off"` leaves GitHub comments untouched and permits authenticated metadata reads.
- `"major-only"` maintains a Critical/Major summary on GitHub alongside the
  Forgejo review.
- `"full"` publishes the review summary only on GitHub, with no Forgejo fallback.

See [publication behavior](../../pkgs/ralph-service/README.md#github-publication)
for head checks, failed reviews, saved delivery intents and mode changes.

Use a token for Ralph's GitHub account that can read the upstream PR and create,
edit and delete its own issue comments. See [GitHub's token permission notes](https://docs.github.com/en/rest/issues/comments).
For an account outside the upstream organization, use a classic personal access
token with `public_repo` scope. Fine-grained tokens owned by the bot account
provide only read access to other owners' public repositories. See
[GitHub's token limitations](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#fine-grained-personal-access-tokens-limitations).
Review stages also send this token when reading GitHub discussions at URLs the
model chooses, so do not give it access to private repositories.
Never put the token itself in Nix configuration or the Nix store.

## Telegram publishing and monitoring

Set `services.ralph.telegram.enable = true` to publish completed reviews and
monitor Ralph in a Telegram channel. It defaults to `false`. Public reports
must also be configured through `reportDir` and `reportBaseUrl`.

```nix
services.ralph.telegram = {
  enable = true;
  botTokenFile = "/run/secrets/ralph/telegram-token";
  chatId = "@ralph_reviews";
};
```

Create a bot with Telegram's `@BotFather` and add it to the channel as an
administrator with permission to post messages. Store its token in the runtime
secret file, readable by the Ralph service user. Never put the token itself in
Nix configuration or the Nix store. For a public channel, `chatId` is its
`@username`. For a private channel, use its numeric chat ID as a string.

The monitor polls Telegram and needs no inbound port. Its separate
`ralph-telegram.service` continues running when the review coordinator stops.
Only channel administrators may issue commands; direct messages are ignored.

Use `/status` for service and queue status, `/review gui 1049` to request a
fresh default-profile review, and `/high gui 1049` for the high profile.
Repository IDs are required; `/help` lists the commands. Requests survive
reviewer downtime. Preserve `stateDir/telegram.sqlite3` with the other state.
See [Telegram operations](../../pkgs/ralph-service/README.md#telegram) for
publication, command authorization and delivery behavior.

## Frozen prompt and routing experiments

Use `ralph-evaluate` to capture a PR and compare configurations against the same
Git objects and discussion evidence. Capture makes no model requests; replay
does. The [evaluation guide](evaluation.md) covers capture, replay, offline
summaries and human scoring. For recorded live usage without an API key, run
`ralph-evaluate usage --state-dir /var/lib/ralph` as the service account.

## Live review progress

The coordinator records routing decisions, model stage boundaries, escalation,
and provider/job retries at journal DEBUG priority. Events identify the repository,
PR, job, generation, and attempt where available. Stage end status `returned`
means the model call returned; response validation and final review coverage are
reported in the saved trace. Conditional stages appear when they start.
Prompts, responses, and tool output are not included in progress events.

Follow normal operational logs with `journalctl -u ralph -p info -f`, or include
live progress with `journalctl -u ralph -p debug -f`. Without a priority filter,
journalctl includes both. Progress uses the existing journal retention policy;
configure disk limits in the consuming host's journald settings.

Individual reviews run in `ralph-job@<job>-<token>.service` units. Inspect them
with `systemctl status 'ralph-job@*.service'` and
`journalctl -u ralph-job@JOB-TOKEN.service`. Stopping `ralph.service` does not
stop active reviews. See [job operations](../../pkgs/ralph-service/README.md#independent-job-operations)
for cancellation and upgrade requirements.
