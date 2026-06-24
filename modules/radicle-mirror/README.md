# Radicle Mirror NixOS Module

Reusable NixOS module for a public Radicle seed, Radicle Explorer frontend, and
a scheduled Bitcoin Core Git mirror.

The module owns Radicle node settings, frontend Caddy routing, mirror users,
systemd units, and mirror state directories. Callers provide public identity,
secret key paths, domains, and delegate policy.

## Usage

```nix
{
  inputs.will-nix.url = "github:willcl-ark/nix";

  outputs =
    { nixpkgs, will-nix, ... }:
    {
      nixosConfigurations.host = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [
          will-nix.nixosModules.radicle-mirror
          {
            services.radicleMirror = {
              enable = true;
              domain = "radicle.example.org";

              seed = {
                enable = true;
                privateKeyFile = "/run/secrets/radicle-private-key";
                publicKey = "ssh-ed25519 ... radicle";
                nodeId = "z6...";
              };

              frontend.enable = true;

              bitcoinMirror = {
                enable = true;
                dataDir = "/var/lib/radicle-mirror";
              };
            };
          }
        ];
      };
    };
}
```

## Interface

- `services.radicleMirror.enable`
- `services.radicleMirror.domain`
- `services.radicleMirror.seed.*`
- `services.radicleMirror.frontend.*`
- `services.radicleMirror.bitcoinMirror.*`

Use `services.radicleMirror.bitcoinMirror.dataDir` to place the mirror identity
and checkout on a host data disk.
