{
  lib,
  stdenvNoCC,
  python3,
  git,
  makeWrapper,
}:

stdenvNoCC.mkDerivation {
  pname = "ralph";
  version = "0.1.0";

  src = ./.;

  nativeBuildInputs = [
    git
    makeWrapper
    python3
  ];

  doCheck = true;
  checkPhase = ''
    runHook preCheck
    export HOME="$TMPDIR"
    export PYTHONDONTWRITEBYTECODE=1
    ${python3.interpreter} -m unittest discover -s . -p 'test_*.py'
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    install -Dm755 review.py $out/libexec/ralph/review.py
    for module in ralph/*.py; do
      install -Dm644 "$module" $out/libexec/ralph/"$module"
    done
    install -Dm644 prompt.md $out/libexec/ralph/prompt.md
    install -Dm644 prompt.md $out/share/ralph/prompt.md
    for audit in audits/*.md; do
      install -Dm644 "$audit" $out/libexec/ralph/"$audit"
      install -Dm644 "$audit" $out/share/ralph/"$audit"
    done
    install -Dm644 audits/models.json $out/libexec/ralph/audits/models.json
    install -Dm644 audits/models.json $out/share/ralph/audits/models.json
    makeWrapper ${python3.interpreter} $out/bin/ralph \
      --add-flags $out/libexec/ralph/review.py \
      --prefix PATH : ${lib.makeBinPath [ git ]}
    runHook postInstall
  '';

  meta = {
    description = "Local committed-change reviewer with selectable review engines";
    mainProgram = "ralph";
    platforms = lib.platforms.unix;
  };
}
