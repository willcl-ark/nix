# CDash

This module runs CDash with nginx, PHP-FPM, a database queue worker, and a
scheduler. By default it creates a PostgreSQL database and role named `cdash`.
The application connects over the local socket using peer authentication.

```nix
{
  imports = [ inputs.nix-collection.nixosModules.cdash ];

  services.cdash = {
    enable = true;
    hostName = "cdash.example.org";
    environmentFile = "/run/secrets/cdash.env";
    settings.APP_URL = "https://cdash.example.org";
  };

  services.nginx.virtualHosts."cdash.example.org" = {
    enableACME = true;
    forceSSL = true;
    extraConfig = "client_max_body_size 100m;";
  };
}
```

The secret file must contain a persistent Laravel application key. Generate
one with `openssl rand -base64 32` and store it as
`APP_KEY=base64:<generated-value>`. Mail credentials and remote database
passwords can go in the same dotenv file. Keep this file outside the Nix store.
The initialization service reads it through systemd credentials and copies it
to a mode-0600 `.env` file owned by `cdash` under `dataDir/app`.
Changing the secret file contents requires restarting `cdash-init`. Its
PHP-FPM pool and queue worker restart with it.

`settings` accepts nonsecret Laravel environment variables. These override
values in the dotenv file. Configure mail through CDash's `MAIL_*` variables.
The GraphQL API is available, but the optional GraphiQL explorer is disabled
because its downloaded assets are not included in the package.

To use an existing PostgreSQL server, set `database.createLocally = false`
and set `DB_HOST`, `DB_PORT`, `DB_DATABASE`, and `DB_USERNAME` through `settings`.
Put `DB_PASSWORD` in the secret file.

On startup, `cdash-init` creates storage directories and runs
`artisan migrate --force`, then refreshes application caches.
Upstream's initial migration creates the schema.
The module does not run the development database seeder, which creates test
accounts. Create an administrator with CDash's `user:save` Artisan command,
run as `cdash` with `APP_BASE_PATH=/var/lib/cdash/app` and PHP
`-d variables_order=EGPCS`. See `artisan help user:save` for its arguments.

Back up PostgreSQL, `dataDir`, and the application key before upgrades.
Migrations run automatically and may change the database schema. Existing
CDash installations older than 4.0 must first follow upstream's upgrade path.

The module defaults to a 100 MiB request and PHP upload limit. The consuming
host configures TLS, ACME, firewall rules, and PHP-FPM pool tuning. Use the
normal nginx virtual host and
`services.phpfpm.pools.cdash` options to configure them.
