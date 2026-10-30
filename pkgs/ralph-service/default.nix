{
  lib,
  stdenvNoCC,
  python3,
  git,
  systemd,
  makeWrapper,
  ralph,
  bubblewrap,
  bash,
  util-linux,
}:

let
  python = python3.withPackages (ps: [
    ps.aiohttp
    ps.markdown-it-py
    ps.nh3
  ]);
in
stdenvNoCC.mkDerivation {
  pname = "ralph-service";
  version = "0.1.0";

  src = ./.;

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
    export PYTHONPATH="${ralph}/libexec/ralph"
    ${python.interpreter} -m unittest discover -s . -p 'test_*.py'
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    for launcher in bot.py evaluate.py followup.py monitor.py; do
      install -Dm755 "$launcher" $out/libexec/ralph-service/"$launcher"
    done
    for module in ralph_service/*.py ralph_service/*.css ralph_service/*.js; do
      install -Dm644 "$module" $out/libexec/ralph-service/"$module"
    done
    install -Dm644 audits/addressed_findings.md $out/libexec/ralph-service/audits/addressed_findings.md
    makeWrapper ${python.interpreter} $out/bin/ralph-service \
      --add-flags $out/libexec/ralph-service/bot.py \
      --prefix PYTHONPATH : ${ralph}/libexec/ralph \
      --prefix PATH : ${
        lib.makeBinPath [
          git
          bash
          util-linux
        ]
      } \
      --set-default RALPH_WRITE_VERIFIER_BWRAP ${lib.getExe bubblewrap} \
      ${lib.optionalString (
        ralph.writeVerifierEnvironment != null
      ) "--set-default RALPH_WRITE_VERIFIER_SHELL ${lib.getExe ralph.writeVerifierEnvironment}"}
    makeWrapper ${python.interpreter} $out/bin/ralph-evaluate \
      --add-flags $out/libexec/ralph-service/evaluate.py \
      --prefix PYTHONPATH : ${ralph}/libexec/ralph \
      --prefix PATH : ${
        lib.makeBinPath [
          git
          bash
          util-linux
        ]
      } \
      --set-default RALPH_WRITE_VERIFIER_BWRAP ${lib.getExe bubblewrap} \
      ${lib.optionalString (
        ralph.writeVerifierEnvironment != null
      ) "--set-default RALPH_WRITE_VERIFIER_SHELL ${lib.getExe ralph.writeVerifierEnvironment}"}
    makeWrapper ${python.interpreter} $out/bin/ralph-followup \
      --add-flags $out/libexec/ralph-service/followup.py \
      --prefix PYTHONPATH : ${ralph}/libexec/ralph \
      --prefix PATH : ${lib.makeBinPath [ git ]}
    makeWrapper ${python.interpreter} $out/bin/ralph-telegram \
      --add-flags $out/libexec/ralph-service/monitor.py \
      --prefix PYTHONPATH : ${ralph}/libexec/ralph \
      --prefix PATH : ${
        lib.makeBinPath [
          git
          systemd
        ]
      }
    runHook postInstall
  '';

  meta = {
    description = "Forgejo review service and evaluation consumers for Ralph";
    mainProgram = "ralph-service";
    platforms = lib.platforms.unix;
  };
}
