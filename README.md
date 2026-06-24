# Nix Packages and Modules

Reusable NixOS modules and, eventually, package definitions for services I run
or maintain.

This repository is intentionally a collection repo: each service stays in its
own directory, but consumers can import just the module they need through a
stable named flake output.

## Modules

| Output | Option prefix | Purpose |
| --- | --- | --- |
| `nixosModules.bitcoin-dnsseed` | `services.bitcoinDnsSeed` | Bitcoin DNS seed deployment using `dnsseedrs`, CoreDNS, DNSSEC key material, optional Tor/I2P proxies, and Caddy seed dumps. |
| `nixosModules.bitcoin-core-guix-substitutes` | `services.bitcoinCoreGuixSubstitutes` | Bitcoin Core Guix substitute publisher with `guix publish`, scheduled builds, signing-key publication, and Caddy wiring. |
| `nixosModules.forgejo-site` | `services.forgejoSite` | Forgejo site deployment with Caddy, optional Anubis, sops-managed secrets, mailer settings, and initial admin bootstrap. |
| `nixosModules.radicle-mirror` | `services.radicleMirror` | Public Radicle seed, Radicle Explorer frontend, and scheduled Bitcoin Core Git mirror. |
| `nixosModules.stuntman` | `services.stuntman` | STUNTMAN STUN server plus the `btcpunch` UDP rendezvous helper. |

## Usage

Add this repository as a flake input:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

    will-nix = {
      url = "github:willcl-ark/nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };
}
```

Then import only the module needed by the host:

```nix
{
  outputs =
    {
      nixpkgs,
      will-nix,
      ...
    }:
    {
      nixosConfigurations.host = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [
          will-nix.nixosModules.stuntman
          {
            services.stuntman.enable = true;
          }
        ];
      };
    };
}
```

The flake does not expose a default NixOS module. Importing modules explicitly
keeps hosts from accidentally depending on unrelated services.

## Layout

```text
modules/
  bitcoin-core-guix-substitutes/
  bitcoin-dnsseed/
  forgejo-site/
  radicle-mirror/
  stuntman/
```

Each module directory may contain:

- `nixos/`: the module implementation.
- `examples/`: small host snippets showing expected configuration.
- `README.md`: service-specific options and notes.
- `scripts/`: runtime helpers used by the module.

Future custom derivations should live under `pkgs/` and be exposed through
`packages.<system>.<name>`.

## Development

Format the repo with:

```sh
nix fmt
```

Inspect the public flake API with:

```sh
nix flake show
```
