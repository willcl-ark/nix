{
  lib,
  mkShell,
  ccacheStdenv,
  writeShellScriptBin,
  cmake,
  ninja,
  pkg-config,
  git,
  python3,
  boost,
  libevent,
  sqlite,
  zeromq,
  qrencode,
  qt6,
  xvfb-run,
  makeFontsConf,
  dejavu_fonts,
}:

let
  qtPackages = [
    qt6.qtbase
    qt6.qtdeclarative
    qt6.qtsvg
  ];
  environment =
    ((mkShell.override { stdenv = ccacheStdenv; }) {
      nativeBuildInputs = [
        cmake
        ninja
        pkg-config
        git
        python3
        qt6.qttools
        xvfb-run
      ];
      buildInputs = [
        boost
        libevent
        sqlite
        zeromq
        qrencode
      ]
      ++ qtPackages;
    }).overrideAttrs
      (_: {
        name = "ralph-gui-verifier-environment";
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
(writeShellScriptBin "ralph-gui-verifier-shell" ''
  source ${environment}/environment
  export QT_PLUGIN_PATH=${lib.makeSearchPath qt6.qtbase.qtPluginPrefix qtPackages}
  export QML_IMPORT_PATH=${lib.makeSearchPath qt6.qtbase.qtQmlPrefix qtPackages}
  export QML2_IMPORT_PATH="$QML_IMPORT_PATH"
  export QT_QPA_PLATFORM=offscreen
  export QT_QUICK_BACKEND=software
  export FONTCONFIG_FILE=${makeFontsConf { fontDirectories = [ dejavu_fonts ]; }}
  exec "$@"
'').overrideAttrs
  {
    meta = {
      description = "Pinned Qt6 build and headless test environment for Ralph GUI experiments";
      mainProgram = "ralph-gui-verifier-shell";
      platforms = lib.platforms.linux;
    };
  }
