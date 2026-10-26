{
  services.cli-proxy-api = {
    enable = true;
    # Supply this file using your host's secret manager, owned by cli-proxy-api.
    configFile = "/run/secrets/cli-proxy-api.yaml";
  };
}
