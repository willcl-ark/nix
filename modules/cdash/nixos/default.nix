{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.cdash;
  appRoot = "${cfg.dataDir}/app";
  source = "${cfg.package}/share/php/cdash";
  php = "${cfg.package.phpPackage}/bin/php";
  artisan = "${php} -d variables_order=EGPCS ${appRoot}/artisan";
  environment = cfg.settings // {
    APP_BASE_PATH = appRoot;
  };
  service = {
    inherit environment;
    requires = [ "cdash-init.service" ];
    after = [ "cdash-init.service" ];
    partOf = [ "cdash-init.service" ];
    serviceConfig = {
      User = "cdash";
      Group = "cdash";
      WorkingDirectory = appRoot;
      UMask = "0027";
    };
  };
in
{
  options.services.cdash = {
    enable = lib.mkEnableOption "CDash build dashboard";
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../../../pkgs/cdash { };
      defaultText = lib.literalExpression "pkgs.callPackage ../../../pkgs/cdash { }";
      description = "CDash package, including its phpPackage passthru attribute.";
    };
    dataDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/cdash";
      description = "Directory for application storage and runtime configuration.";
    };
    hostName = lib.mkOption {
      type = lib.types.str;
      description = "Hostname of the nginx virtual host.";
    };
    environmentFile = lib.mkOption {
      type = lib.types.path;
      description = ''
        Runtime Laravel dotenv file containing APP_KEY and other secrets.
        It is copied into the private application directory on startup.
        Do not use a Nix store path for secrets.
      '';
    };
    settings = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      description = "Nonsecret Laravel environment settings. These override the dotenv file.";
    };
    database.createLocally = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Create a local PostgreSQL database and cdash role using peer authentication.";
    };
  };

  config = lib.mkIf cfg.enable {
    services.cdash.settings = {
      APP_ENV = lib.mkDefault "production";
      APP_DEBUG = lib.mkDefault "false";
      APP_URL = lib.mkDefault "http://${cfg.hostName}";
      DB_HOST = lib.mkIf cfg.database.createLocally (lib.mkDefault "/run/postgresql");
      DB_DATABASE = lib.mkDefault "cdash";
      DB_USERNAME = lib.mkDefault "cdash";
      QUEUE_CONNECTION = lib.mkDefault "database";
      GRAPHIQL_ENABLED = lib.mkDefault "false";
    };

    users.users.cdash = {
      isSystemUser = true;
      group = "cdash";
      home = cfg.dataDir;
    };
    users.groups.cdash = { };
    systemd.tmpfiles.rules = [ "d ${cfg.dataDir} 0750 cdash cdash -" ];

    services.postgresql = lib.mkIf cfg.database.createLocally {
      enable = true;
      ensureDatabases = [ "cdash" ];
      ensureUsers = [
        {
          name = "cdash";
          ensureDBOwnership = true;
        }
      ];
    };

    systemd.services.cdash-init = {
      description = "Initialize CDash and migrate its database";
      wantedBy = [ "multi-user.target" ];
      after = lib.optional cfg.database.createLocally "postgresql.service";
      requires = lib.optional cfg.database.createLocally "postgresql.service";
      inherit environment;
      restartTriggers = [
        cfg.package
        (pkgs.writeText "cdash-settings.json" (builtins.toJSON environment))
      ];
      path = [ pkgs.coreutils ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = "cdash";
        Group = "cdash";
        UMask = "0027";
        LoadCredential = "environment:${cfg.environmentFile}";
      };
      script = ''
        mkdir -p ${lib.escapeShellArg appRoot}/bootstrap/cache
        for entry in ${source}/*; do
          name=$(basename "$entry")
          case "$name" in storage|bootstrap) continue ;; esac
          ln -sfn "$entry" ${lib.escapeShellArg appRoot}/"$name"
        done
        ln -sfn ${source}/bootstrap/app.php ${lib.escapeShellArg appRoot}/bootstrap/app.php
        install -m 0600 "$CREDENTIALS_DIRECTORY/environment" ${lib.escapeShellArg appRoot}/.env
        ${artisan} config:clear
        ${artisan} storage:mkdirs
        ${artisan} package:discover
        ${artisan} key:check
        ${artisan} migrate --force
        ${artisan} view:cache
        ${artisan} lighthouse:cache
        ${artisan} config:cache
      '';
    };

    services.phpfpm.pools.cdash = {
      user = "cdash";
      group = "cdash";
      phpPackage = cfg.package.phpPackage;
      phpOptions = ''
        variables_order = EGPCS
        upload_max_filesize = 100M
        post_max_size = 100M
      '';
      phpEnv = environment;
      settings = {
        "listen.owner" = config.services.nginx.user;
        "listen.group" = config.services.nginx.group;
        "listen.mode" = "0660";
        "pm" = "dynamic";
        "pm.max_children" = 16;
        "pm.start_servers" = 2;
        "pm.min_spare_servers" = 1;
        "pm.max_spare_servers" = 4;
        "clear_env" = true;
      };
    };
    systemd.services.phpfpm-cdash = {
      partOf = [ "cdash-init.service" ];
      requires = [ "cdash-init.service" ];
      after = [ "cdash-init.service" ];
    };

    services.nginx = {
      enable = true;
      virtualHosts.${cfg.hostName} = {
        root = "${source}/public";
        extraConfig = lib.mkDefault "client_max_body_size 100m;";
        locations."/" = {
          index = "index.php";
          tryFiles = "$uri $uri/ /index.php?$query_string";
        };
        locations."= /index.php" = {
          extraConfig = ''
            include ${config.services.nginx.package}/conf/fastcgi_params;
            fastcgi_param SCRIPT_FILENAME ${source}/public/index.php;
            fastcgi_pass unix:${config.services.phpfpm.pools.cdash.socket};
          '';
        };
        locations."~ /\\.(?!well-known/)".extraConfig = "deny all;";
      };
    };

    systemd.services.cdash-worker = lib.recursiveUpdate service {
      description = "CDash submission queue worker";
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        ExecStart = "${artisan} queue:work --max-time=3600 --memory=256";
        Restart = "always";
        RestartSec = "5s";
      };
    };
    systemd.services.cdash-scheduler = lib.recursiveUpdate service {
      description = "Run CDash scheduled tasks";
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${artisan} schedule:run";
      };
    };
    systemd.timers.cdash-scheduler = {
      wantedBy = [ "timers.target" ];
      timerConfig.OnCalendar = "*-*-* *:*:00";
    };
  };
}
