# CDash

This module runs CDash with nginx, PHP-FPM, a database queue worker, and a
scheduler. By default it creates a PostgreSQL database and role named `cdash`.
The application connects over the local socket using peer authentication.

Import this module and sops-nix in the consuming host. Supply a persistent
Laravel application key through a sops secret:

```nix
{ config, ... }:
{
  imports = [
    inputs.nix-collection.nixosModules.cdash
    inputs.sops-nix.nixosModules.sops
  ];

  sops.defaultSopsFile = ./secrets.yaml;
  sops.secrets.cdash-app-key.restartUnits = [ "cdash-init.service" ];
  sops.secrets.cdash-smtp-password.restartUnits = [ "cdash-init.service" ];

  services.cdash = {
    enable = true;
    hostName = "cdash.example.org";
    appUrl = "https://cdash.example.org";
    appKeyFile = config.sops.secrets.cdash-app-key.path;
    mail = {
      mailer = "smtp";
      host = "smtp.example.org";
      port = 587;
      fromAddress = "cdash@example.org";
      user = "cdash";
      passwordFile = config.sops.secrets.cdash-smtp-password.path;
    };
  };

  services.nginx.virtualHosts."cdash.example.org" = {
    enableACME = true;
    forceSSL = true;
  };
}
```

Generate the application key with `openssl rand -base64 32` and encrypt
`base64:<generated-value>` as the `cdash-app-key` secret. Keep it across upgrades.
The module accepts runtime secret paths without depending on sops-nix itself.
The initialization service reads them through systemd credentials and writes
a mode-0600 `.env` file owned by `cdash` under `dataDir/app`. Passwords containing
quotes, dollars, or spaces are preserved. Set sops `restartUnits` as above so
secret changes restart `cdash-init`, PHP-FPM, and the queue worker.
If you use the default `sendmail` transport, configure an MTA in the host.
The module uses NixOS's `/run/wrappers/bin/sendmail` path.

## Configuration options

The options below cover upstream's [configuration guide](https://github.com/Kitware/CDash/blob/v5.4.0/docs/config.md).
Defaults follow the packaged release, except for local PostgreSQL peer
credentials and asynchronous submission parsing.

| Module options | CDash variables |
| --- | --- |
| `appUrl`, `appKeyFile` | `APP_URL`, `APP_KEY` |
| `database.host`, `.port`, `.name`, `.user`, `.passwordFile` | `DB_HOST`, `DB_PORT`, `DB_DATABASE`, `DB_USERNAME`, `DB_PASSWORD` |
| `mail.mailer`, `.fromAddress`, `.replyAddress`, `.fromName` | `MAIL_MAILER`, `MAIL_FROM_ADDRESS`, `MAIL_REPLY_ADDRESS`, `MAIL_FROM_NAME` |
| `mail.host`, `.port`, `.user`, `.encryption`, `.passwordFile` | `MAIL_HOST`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_ENCRYPTION`, `MAIL_PASSWORD` |
| `submissionParsing` | `QUEUE_CONNECTION`, either `"database"` or `"sync"` |
| `storage.driver`, `.bucket`, `.region` | `FILESYSTEM_DRIVER`, `AWS_BUCKET`, `AWS_DEFAULT_REGION` |
| `storage.usePathStyleEndpoint`, `.endpoint`, `.url` | `AWS_USE_PATH_STYLE_ENDPOINT`, `AWS_ENDPOINT`, `AWS_URL` |
| `storage.accessKeyIdFile`, `.secretAccessKeyFile` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` |
| `backupTimeframe`, `defaultProject`, `largeTextLimit`, `useVcsApi` | `BACKUP_TIMEFRAME`, `DEFAULT_PROJECT`, `LARGE_TEXT_LIMIT`, `USE_VCS_API` |

To use an existing PostgreSQL server, set `database.createLocally = false`
and configure the typed database options. Put its password in a sops secret
and reference its path through `database.passwordFile`. Local provisioning
requires the database and role names to remain `cdash`.

For S3 or MinIO, set `storage.driver = "s3"`, choose the bucket and region,
and provide credential paths. MinIO typically also needs
`usePathStyleEndpoint = true`, `endpoint`, and `url`.
The release's filesystem configuration reads
`AWS_DEFAULT_REGION`, despite the upstream guide naming `AWS_REGION`; the
module maps `storage.region` to the variable that the application uses.

`submissionParsing = "sync"` disables the queue worker. The default
`"database"` keeps a supervised worker running for asynchronous parsing.
`backupTimeframe` is measured in hours; zero disables parsed-file retention.
`largeTextLimit` is measured in bytes; zero allows unlimited text.

`settings` accepts additional nonsecret environment variables, such as LDAP,
OAuth, registration, and project policy from [`.env.example`](https://github.com/Kitware/CDash/blob/v5.4.0/.env.example).
Explicit `settings` entries override the typed option mappings. An optional
`environmentFile` accepts a supplementary sops-managed dotenv file for other
secret variables; dedicated secret file options override matching entries.
The GraphQL API is available, but the optional GraphiQL explorer is disabled
because its downloaded assets are not included in the package.

## Operation

On startup, `cdash-init` creates storage directories, runs
`artisan migrate --force`, and refreshes application caches.
Upstream's initial migration creates the schema. The module does not run the
development database seeder, which creates test accounts. Create an
administrator with CDash's `user:save` Artisan command, run as `cdash` with
`APP_BASE_PATH=/var/lib/cdash/app` and PHP `-d variables_order=EGPCS`.
See `artisan help user:save` for its arguments.

Back up PostgreSQL, `dataDir`, and the application key before upgrades.
Migrations run automatically and may change the database schema. Existing
CDash installations older than 4.0 must first follow upstream's upgrade path.

The module defaults to a 100 MiB request and PHP upload limit. The consuming
host configures TLS, ACME, firewall rules, and PHP-FPM pool tuning through the
normal nginx virtual host and `services.phpfpm.pools.cdash` options.
