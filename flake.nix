{
  description = "Reusable NixOS modules and packages";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  inputs.bix = {
    url = "github:bitcoin-dev-tools/bix/eef14aeccfca9af88ab2a60487e71ab5fdcb123e";
    inputs.nixpkgs.follows = "nixpkgs";
  };
  inputs.llm-agents = {
    url = "github:numtide/llm-agents.nix";
    inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    {
      self,
      nixpkgs,
      llm-agents,
      bix,
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
          bixShell = bix.devShells.${system}.default;
        })
        // {
          cli-proxy-api =
            (nixpkgs.legacyPackages.${system}.extend llm-agents.overlays.shared-nixpkgs)
            .llm-agents.cli-proxy-api;
        }
      );

      nixosModules = (import ./modules) // {
        ralph =
          { lib, pkgs, ... }:
          {
            imports = [ ./modules/ralph/nixos ];
            services.ralph.package =
              lib.mkDefault
                self.packages.${pkgs.stdenv.hostPlatform.system}.ralph-service;
            services.ralph.writeVerifierEnvironment =
              lib.mkDefault
                self.packages.${pkgs.stdenv.hostPlatform.system}.ralph-write-verifier-env;
          };
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
