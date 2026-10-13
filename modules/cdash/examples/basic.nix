{ config, ... }:
{
  # Import sops-nix and configure its encrypted secret file in the host.
  sops.secrets.cdash-app-key.restartUnits = [ "cdash-init.service" ];

  services.cdash = {
    enable = true;
    hostName = "cdash.example.org";
    appUrl = "https://cdash.example.org";
    appKeyFile = config.sops.secrets.cdash-app-key.path;
  };

  services.nginx.virtualHosts."cdash.example.org" = {
    enableACME = true;
    forceSSL = true;
  };
}
