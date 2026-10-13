{ pkgs }:

{
  cdash = pkgs.callPackage ./cdash { };
  dnsseedrs = pkgs.callPackage ./dnsseedrs { };
  ralph = pkgs.callPackage ./ralph { };
}
