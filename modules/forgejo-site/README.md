# Forgejo Site NixOS Module

Reusable NixOS module for a public Forgejo deployment with Caddy, optional
Anubis, sops-managed secrets, and initial admin bootstrap.

Callers provide site-local identity, domain, mailer settings, and secret names.
The module owns the Forgejo settings, Caddy route, Anubis route, secret
ownership, and admin initialization unit.

## Usage

```nix
{
  services.forgejoSite = {
    enable = true;
    domain = "code.example.org";
    dataDir = "/var/lib/forgejo";

    admin = {
      user = "admin";
      email = "admin@example.org";
    };

    mailer = {
      enable = true;
      from = "Forgejo <forgejo@example.org>";
      smtpAddress = "smtp.example.org";
      user = "forgejo@example.org";
    };
  };
}
```

## Interface

- `services.forgejoSite.enable`
- `services.forgejoSite.domain`
- `services.forgejoSite.dataDir`
- `services.forgejoSite.package`
- `services.forgejoSite.admin.*`
- `services.forgejoSite.http.*`
- `services.forgejoSite.secrets.*`
- `services.forgejoSite.mailer.*`
- `services.forgejoSite.anubis.*`
