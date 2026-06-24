{ dnsseedrs }:

{
  bitcoin-core-guix-substitutes = ./bitcoin-core-guix-substitutes/nixos;
  bitcoin-dnsseed = import ./bitcoin-dnsseed/nixos { inherit dnsseedrs; };
  forgejo-site = ./forgejo-site/nixos;
  radicle-mirror = ./radicle-mirror/nixos;
  stuntman = ./stuntman/nixos;
}
