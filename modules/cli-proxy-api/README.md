# CLIProxyAPI

Import `will-nix.nixosModules.cli-proxy-api` and enable the service:

```nix
services.cli-proxy-api = {
  enable = true;
  configFile = "/run/secrets/cli-proxy-api.yaml";
};
```

The module uses the CLIProxyAPI package from `numtide/llm-agents.nix`.
Override `services.cli-proxy-api.package` to use another build.

## Runtime configuration

Provision the YAML file through the host's secret manager with owner
`cli-proxy-api` and mode `0400`. Use a string path as above so Nix does not
copy its contents into the store. This configuration uses the pinned
CLIProxyAPI v8 schema:

```yaml
config-version: 8
server:
  host: "127.0.0.1"
  port: 8317
management:
  secret-key: ""
  disable-control-panel: true
access:
  api-keys:
    - "REPLACE_WITH_A_RANDOM_LOCAL_CLIENT_KEY"
oauth:
  auth-dir: "/var/lib/cli-proxy-api/auth"
```

Set `server.host` explicitly. Upstream binds all interfaces when it is empty;
the module passes the file through and does not override its listener. The
module does not open firewall ports. The empty management key disables the
management API.

For quota polling or the built-in UI, set a separate nonempty
`management.secret-key`, keep `management.allow-remote: false`, and retain the
loopback listener. Client keys in `access.api-keys` cannot sign into management.
Set `management.disable-control-panel: false` to serve `/management.html`.
No separate frontend deployment is needed. The pinned package downloads the
panel when needed; the module directs its cache to `dataDir/static` through
`MANAGEMENT_STATIC_PATH` so it can write under the service sandbox.

The client key authenticates requests from Ralph to the local proxy. It is
separate from the subscription OAuth tokens. Give Ralph the same client key
through its own secret file. Do not put upstream OpenAI API keys in this
configuration when testing subscription access.

The service has a dedicated `cli-proxy-api` user and a private persistent home
at `/var/lib/cli-proxy-api`. If you change `dataDir`, update `oauth.auth-dir`
to match. The service can write only beneath `dataDir`, apart from its private
temporary directory.

## Codex login

After creating the service user and state directory through a host rebuild,
run the packaged binary as the service user. Use the same config path as the
service so the login writes into its credential directory:

```sh
nix build .#cli-proxy-api
sudo -u cli-proxy-api env HOME=/var/lib/cli-proxy-api \
  ./result/bin/cli-proxy-api \
  --config /run/secrets/cli-proxy-api.yaml --codex-device-login
```

Follow the device login instructions in your browser. This is a one-time
operator login, not a coding CLI process per review. The proxy stores and
refreshes OAuth tokens in `oauth.auth-dir`. Treat that directory as a secret.
If device login is unavailable for the account, the same binary supports
`--codex-login --no-browser` for the browser callback flow.

Restart the service after the first login and inspect its logs:

```sh
sudo systemctl restart cli-proxy-api
sudo journalctl -u cli-proxy-api -n 50
```

Use `http://127.0.0.1:8317/v1` as the local OpenAI-compatible API base.
The authenticated `/v1/models` endpoint lists the model IDs available to the
account. Subscription quotas still apply. This module does not provide an
automatic fallback to paid API access.
