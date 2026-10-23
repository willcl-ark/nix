{ pkgs }:

rec {
  cdash = pkgs.callPackage ./cdash { };
  dnsseedrs = pkgs.callPackage ./dnsseedrs { };
  ralph = pkgs.callPackage ./ralph { };
  ralph-service = pkgs.callPackage ./ralph-service { inherit ralph; };
}
