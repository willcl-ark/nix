{
  lib,
  git,
  writeShellScriptBin,
  bixShell,
}:

let
  # Freeze the devShell's setup-hook results while building the package. The
  # experiment can then use its compiler and libraries without a Nix daemon.
  environment = bixShell.overrideAttrs (old: {
    name = "ralph-write-verifier-environment";
    nativeBuildInputs = old.nativeBuildInputs ++ [ git ];
    buildPhase = ''
      mkdir -p "$out"
      destination="$out/environment"
      (
        unset HOME TMPDIR TMP TEMP TEMPDIR PWD OLDPWD SHLVL NIX_BUILD_TOP NIX_LOG_FD
        export -p
      ) > "$destination"
    '';
  });
in
(writeShellScriptBin "ralph-write-verifier-shell" ''
  source ${environment}/environment
  # Run machine-dependent shell hooks on the experiment host, not the builder.
  eval "$shellHook"
  exec "$@"
'').overrideAttrs
  {
    meta = {
      description = "Pinned Bix build environment for Ralph experiments";
      mainProgram = "ralph-write-verifier-shell";
      platforms = lib.platforms.linux;
    };
  }
