{
  services.cdash = {
    enable = true;
    hostName = "cdash.example.org";
    environmentFile = "/run/secrets/cdash.env";
    settings.APP_URL = "https://cdash.example.org";
  };

  services.nginx.virtualHosts."cdash.example.org" = {
    enableACME = true;
    forceSSL = true;
    extraConfig = "client_max_body_size 100m;";
  };
}
