{
  lib,
  stdenvNoCC,
  python3,
  git,
  makeWrapper,
  bubblewrap,
  bash,
  util-linux,
  writeVerifierEnvironment ? null,
}:

let
  python = python3.withPackages (ps: [ ps.aiohttp ]);
in
stdenvNoCC.mkDerivation {
  pname = "ralph";
  version = "0.1.0";

  src = ./.;
  passthru = { inherit writeVerifierEnvironment; };

  nativeBuildInputs = [
    git
    makeWrapper
    python
  ];

  doCheck = true;
  checkPhase = ''
    runHook preCheck
    export HOME="$TMPDIR"
    export PYTHONDONTWRITEBYTECODE=1
    ${python.interpreter} -m unittest discover -s tests -p 'test_*.py'
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    install -Dm755 review.py $out/libexec/ralph/review.py
    for module in ralph/*.py; do
      install -Dm644 "$module" $out/libexec/ralph/"$module"
    done
    cp -r agents final_comment $out/libexec/ralph/
    mkdir -p $out/share/ralph
    cp -r agents final_comment $out/share/ralph/
    makeWrapper ${python.interpreter} $out/bin/ralph \
      --add-flags $out/libexec/ralph/review.py \
      --prefix PATH : ${
        lib.makeBinPath [
          git
          bash
          util-linux
        ]
      } \
      --set-default RALPH_WRITE_VERIFIER_BWRAP ${lib.getExe bubblewrap} \
      ${lib.optionalString (
        writeVerifierEnvironment != null
      ) "--set-default RALPH_WRITE_VERIFIER_SHELL ${lib.getExe writeVerifierEnvironment}"}
    runHook postInstall
  '';

  meta = {
    description = "Local committed-change reviewer with selectable review engines";
    mainProgram = "ralph";
    platforms = lib.platforms.unix;
  };
}
