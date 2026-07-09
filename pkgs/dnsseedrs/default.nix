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
    rev = "b8b76c5b21fddaa4fb625bbd2b82935fdcc95056";
    hash = "sha256-heysxwpN/3VWAfP9JmRq3UHtlvn+8czwHsEXFABjM/I=";
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
