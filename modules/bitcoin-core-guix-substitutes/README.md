# Bitcoin Core Guix Substitutes NixOS Module

Reusable NixOS module for publishing Bitcoin Core Guix build substitutes.

The module owns the Guix daemon, `guix publish`, the Bitcoin Core checkout,
build timers, public signing-key files, and Caddy virtual host wiring. Callers
provide the public domain, the persistent data directory, and signing-key
inputs.

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
          will-nix.nixosModules.bitcoin-core-guix-substitutes
          {
            services.bitcoinCoreGuixSubstitutes = {
              enable = true;
              domain = "guix.example.org";
              dataDir = "/gnu/guix-bitcoin";

              signingKey = {
                publicFile = ./secrets/signing-key.pub;
                privateFile = ./secrets/signing-key.sec;
                signatureFile = ./secrets/signing-key.pub.asc;
              };
            };
          }
        ];
      };
    };
}
```

## Interface

- `services.bitcoinCoreGuixSubstitutes.enable`
- `services.bitcoinCoreGuixSubstitutes.domain`
- `services.bitcoinCoreGuixSubstitutes.dataDir`
- `services.bitcoinCoreGuixSubstitutes.storeDir`
- `services.bitcoinCoreGuixSubstitutes.signingKey.*`
- `services.bitcoinCoreGuixSubstitutes.buildJobs`
- `services.bitcoinCoreGuixSubstitutes.macosSdks`
- `services.bitcoinCoreGuixSubstitutes.buildTimer.*`
- `services.bitcoinCoreGuixSubstitutes.cleanup.*`

The module derives its publish cache and public key directory from `dataDir`.
