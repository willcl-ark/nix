{
  lib,
  fetchFromGitHub,
  buildNpmPackage,
  nodejs_24,
  php84,
}:
let
  version = "5.4.0";
  src = fetchFromGitHub {
    owner = "Kitware";
    repo = "CDash";
    tag = "v${version}";
    hash = "sha256-swpds/aCKHLl0fPwNNTAClzJXQsT6fL/wXEALGb7160=";
  };
  php = php84.buildEnv {
    extensions =
      { enabled, all }:
      enabled
      ++ (with all; [
        bcmath
        bz2
        gd
        ldap
        pdo_pgsql
        xsl
      ]);
  };
  frontend = buildNpmPackage {
    pname = "cdash-frontend";
    inherit version src;
    nodejs = nodejs_24;
    npmDepsHash = "sha256-c9RRd1rFRZ4rZT/KKfTtKlGQesJc7Tak1hK8TFIjSUs=";
    npmFlags = [ "--omit=dev" ];
    npmBuildScript = "prod";
    postPatch = ''
      substituteInPlace webpack.mix.js \
        --replace-fail "new Date().getTime().toString()" "'${version}'"
    '';
    installPhase = ''
      runHook preInstall
      mkdir -p $out
      cp -r public/. $out/
      runHook postInstall
    '';
  };
in
php.buildComposerProject2 {
  pname = "cdash";
  inherit version src;
  vendorHash = "sha256-SOajk0HHbr3y+AhDSeRRXoTlP1X3aTmFeY74O80pM3c=";
  postBuild = ''
    cp -r ${frontend}/. public/
    printf 'v${version}\n' > public/VERSION
  '';
  postInstall = ''
    rm -rf $out/share/php/cdash/{tests,docker,.github}
  '';
  passthru.phpPackage = php;
  meta = {
    description = "Web dashboard for CTest build and test results";
    homepage = "https://www.cdash.org";
    license = lib.licenses.bsd3;
  };
}
