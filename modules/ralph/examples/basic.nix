{
  services.ralph = {
    enable = true;
    openaiApiBase = "http://127.0.0.1:8317/v1";
    openaiKeyFile = "/run/secrets/ralph/proxy-key";
    repositories.core = {
      origin = "https://git.example.org/owner/repo.git";
      repository = "owner/repo";
      forgejoApi = "https://git.example.org/api/v1/repos/owner/repo";
      botLogin = "ralph";
      webhookSecretFile = "/run/secrets/ralph/webhook-secret";
      forgejoTokenFile = "/run/secrets/ralph/forgejo-token";
      workers = 3;
    };
  };
}
