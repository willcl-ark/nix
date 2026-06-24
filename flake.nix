{
  description = "Reusable NixOS modules and packages";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    dnsseedrs = {
      url = "github:willcl-ark/dnsseedrs";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      dnsseedrs,
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
      nixosModules = import ./modules { inherit dnsseedrs; };

      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt-tree);
    };
}
