{
  description = "Reusable NixOS modules and packages";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  inputs.llm-agents = {
    url = "github:numtide/llm-agents.nix";
    inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    {
      self,
      nixpkgs,
      llm-agents,
      ...
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
        (import ./pkgs {
          pkgs = nixpkgs.legacyPackages.${system};
        })
        // {
          cli-proxy-api =
            (nixpkgs.legacyPackages.${system}.extend llm-agents.overlays.shared-nixpkgs)
            .llm-agents.cli-proxy-api;
        }
      );

      nixosModules = (import ./modules) // {
        cli-proxy-api =
          { lib, pkgs, ... }:
          {
            imports = [ ./modules/cli-proxy-api/nixos ];
            services.cli-proxy-api.package =
              lib.mkDefault
                self.packages.${pkgs.stdenv.hostPlatform.system}.cli-proxy-api;
          };
      };

      devShells = forAllSystems (system: {
        default = nixpkgs.legacyPackages.${system}.mkShell {
          packages = [ nixpkgs.legacyPackages.${system}.ruff ];
        };
      });

      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt-tree);
    };
}
