# STUNTMAN NixOS Module

Reusable NixOS module for running a STUNTMAN STUN server and the btcpunch UDP
rendezvous helper.

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
          will-nix.nixosModules.stuntman
          {
            services.stuntman.enable = true;
          }
        ];
      };
    };
}
```

## Options

- `services.stuntman.enable`
- `services.stuntman.stunPort`
- `services.stuntman.rendezvousAddress`
- `services.stuntman.rendezvousPort`
- `services.stuntman.openFirewall`
