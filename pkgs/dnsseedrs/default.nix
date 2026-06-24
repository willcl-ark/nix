{
  lib,
  rustPlatform,
  fetchFromGitHub,
  pkg-config,
  sqlite,
  darwin,
  stdenv,
}:

rustPlatform.buildRustPackage rec {
  pname = "dnsseedrs";
  version = "0.2.0";

  src = fetchFromGitHub {
    owner = "willcl-ark";
    repo = "dnsseedrs";
    rev = "c57bc4677b4ca8acb0a4cdb1b2eba9c1e673fa50";
    hash = "sha256-w0I4qBnDaoXc+6Ya7Xy+M3hai4bkJMQ61jnVQz6c6HA=";
  };

  cargoHash = "sha256-IMAhmMENMbZO+S57atNHpibr46vSlj1g7zYeQ2Djo2w=";

  nativeBuildInputs = [ pkg-config ];
  buildInputs = [
    sqlite
  ]
  ++ lib.optionals stdenv.hostPlatform.isDarwin [
    darwin.apple_sdk.frameworks.Security
    darwin.apple_sdk.frameworks.SystemConfiguration
  ];

  meta = {
    description = "Bitcoin DNS seeder";
    homepage = "https://github.com/willcl-ark/dnsseedrs";
    mainProgram = "dnsseedrs";
    license = lib.licenses.mit;
  };
}
