{
  services.ralph = {
    enable = true;
    # The default package is ralph-service; main review prompts come from its core.
    # Set followupPromptFile and followupModel separately to customize follow-up.
    origin = "https://git.example.org/owner/repo.git";
    repository = "owner/repo";
    forgejoApi = "https://git.example.org/api/v1/repos/owner/repo";
    botLogin = "ralph";

    openaiApiBase = "http://127.0.0.1:8317/v1";
    openaiKeyFile = "/run/secrets/ralph/proxy-key";
    webhookSecretFile = "/run/secrets/ralph/webhook-secret";
    forgejoTokenFile = "/run/secrets/ralph/forgejo-token";
  };
}
