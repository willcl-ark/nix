{ inputs, pkgs, ... }:
{
  services.ralph = {
    enable = true;
    workers = 3;
    openaiKeyFile = "/run/secrets/ralph/proxy-key";
    reportDir = "/var/www/ralph";
    reportBaseUrl = "https://reviews.example.org";
    repositories = {
      core = {
        origin = "https://git.example.org/bitcoin/bitcoin.git";
        repository = "bitcoin/bitcoin";
        forgejoApi = "https://git.example.org/api/v1/repos/bitcoin/bitcoin";
        botLogin = "ralph";
        webhookSecretFile = "/run/secrets/ralph/core-webhook";
        forgejoTokenFile = "/run/secrets/ralph/forgejo-token";
        githubPublication = "off";
        workers = 2;
      };
      gui = {
        origin = "https://git.example.org/bitcoin-core/gui-qml.git";
        repository = "bitcoin-core/gui-qml";
        forgejoApi = "https://git.example.org/api/v1/repos/bitcoin-core/gui-qml";
        botLogin = "ralph";
        webhookSecretFile = "/run/secrets/ralph/gui-webhook";
        forgejoTokenFile = "/run/secrets/ralph/forgejo-token";
        githubRepository = "bitcoin-core/gui-qml";
        githubTokenFile = "/run/secrets/ralph/gui-github-token";
        githubPublication = "full";
        strategy = "gui";
        submodules.bitcoin = "https://github.com/bitcoin/bitcoin";
        writeVerifierEnvironment =
          inputs.will-nix.packages.${pkgs.stdenv.hostPlatform.system}.ralph-gui-verifier-env;
      };
    };
  };
}
