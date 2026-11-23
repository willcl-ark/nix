{
  lib,
  stdenvNoCC,
  python3,
  git,
  systemd,
  nix,
  makeWrapper,
  ralph,
  bubblewrap,
  bash,
  util-linux,
  jetbrains-mono,
}:

let
  python = python3.withPackages (ps: [
    ps.aiohttp
    ps.markdown-it-py
    ps.nh3
  ]);
  fontTools = python3.withPackages (ps: [
    ps.fonttools
    ps.brotli
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
    ${python.interpreter} -m unittest discover -s tests -p 'test_*.py'
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    for launcher in bot.py job.py evaluate.py followup.py monitor.py feedback.py post_mortem.py; do
      install -Dm755 "$launcher" $out/libexec/ralph-service/"$launcher"
    done
    for module in ralph_service/*.py ralph_service/*.css ralph_service/*.js; do
      install -Dm644 "$module" $out/libexec/ralph-service/"$module"
    done
    for audit in audits/*.md; do
      install -Dm644 "$audit" $out/libexec/ralph-service/"$audit"
    done
    mkdir -p $out/share/ralph-service/fonts
    ${fontTools}/bin/pyftsubset "${jetbrains-mono}/share/fonts/truetype/JetBrainsMono[wght].ttf" \
      --flavor=woff2 \
      --output-file=$out/share/ralph-service/fonts/ralph-mono.woff2 \
      --unicodes=U+0020-007E,U+00A0-00FF,U+2010-2027,U+2030-203A,U+2190-2199,U+2500-259F,U+25A0-25FF,U+2713,U+2717 \
      --layout-features=kern,zero
    makeWrapper ${python.interpreter} $out/bin/ralph-service \
      --add-flags $out/libexec/ralph-service/bot.py \
      --set-default RALPH_JOB_EXECUTABLE "$out/bin/ralph-job" \
      --prefix PYTHONPATH : ${ralph}/libexec/ralph \
      --prefix PATH : ${
        lib.makeBinPath [
          git
          bash
          util-linux
          systemd
          nix
        ]
      } \
      --set-default RALPH_WRITE_VERIFIER_BWRAP ${lib.getExe bubblewrap} \
      ${lib.optionalString (
        ralph.writeVerifierEnvironment != null
      ) "--set-default RALPH_WRITE_VERIFIER_SHELL ${lib.getExe ralph.writeVerifierEnvironment}"}
    makeWrapper ${python.interpreter} $out/bin/ralph-job \
      --add-flags $out/libexec/ralph-service/job.py \
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
    makeWrapper ${python.interpreter} $out/bin/ralph-post-mortem \
      --add-flags $out/libexec/ralph-service/post_mortem.py \
      --prefix PYTHONPATH : ${ralph}/libexec/ralph \
      --prefix PATH : ${lib.makeBinPath [ git ]}
    makeWrapper ${python.interpreter} $out/bin/ralph-feedback \
      --add-flags $out/libexec/ralph-service/feedback.py \
      --prefix PYTHONPATH : ${ralph}/libexec/ralph
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
