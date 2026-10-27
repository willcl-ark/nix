{ pkgs, bixShell }:

rec {
  cdash = pkgs.callPackage ./cdash { };
  dnsseedrs = pkgs.callPackage ./dnsseedrs { };
  ralph-write-verifier-env = pkgs.callPackage ./ralph-write-verifier-env { inherit bixShell; };
  ralph = pkgs.callPackage ./ralph { writeVerifierEnvironment = ralph-write-verifier-env; };
  ralph-service = pkgs.callPackage ./ralph-service { inherit ralph; };
}
