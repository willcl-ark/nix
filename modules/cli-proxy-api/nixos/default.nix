{
  config,
  lib,
  ...
}:
let
  cfg = config.services.cli-proxy-api;
in
{
  options.services.cli-proxy-api = {
    enable = lib.mkEnableOption "CLIProxyAPI subscription inference proxy";

    package = lib.mkOption {
      type = lib.types.package;
      description = "CLIProxyAPI package. The named flake module supplies the default.";
    };

    configFile = lib.mkOption {
      type = lib.types.str;
      example = "/run/secrets/cli-proxy-api.yaml";
      description = ''
        Absolute path to the runtime YAML configuration, readable by the
        cli-proxy-api user. Keep client API keys outside the Nix store.
        Set server.host explicitly to 127.0.0.1 for local access and
        oauth.auth-dir to a directory beneath dataDir for persistent logins.
      '';
    };

    dataDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/cli-proxy-api";
      description = "Persistent service home for OAuth credentials and other writable state.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.hasPrefix "/" cfg.configFile;
        message = "services.cli-proxy-api.configFile must be an absolute runtime path.";
      }
      {
        assertion = lib.hasPrefix "/" cfg.dataDir;
        message = "services.cli-proxy-api.dataDir must be absolute.";
      }
    ];

    users.users.cli-proxy-api = {
      isSystemUser = true;
      group = "cli-proxy-api";
      home = cfg.dataDir;
    };
    users.groups.cli-proxy-api = { };

    systemd.tmpfiles.rules = [ "d ${cfg.dataDir} 0700 cli-proxy-api cli-proxy-api -" ];

    systemd.services.cli-proxy-api = {
      description = "CLIProxyAPI subscription inference proxy";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      environment = {
        HOME = cfg.dataDir;
        MANAGEMENT_STATIC_PATH = "${cfg.dataDir}/static";
      };
      serviceConfig = {
        ExecStart = lib.escapeShellArgs [
          (lib.getExe cfg.package)
          "--config"
          cfg.configFile
        ];
        User = "cli-proxy-api";
        Group = "cli-proxy-api";
        WorkingDirectory = cfg.dataDir;
        UMask = "0077";
        Restart = "on-failure";
        RestartSec = "5s";
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ cfg.dataDir ];
      };
    };
  };
}
