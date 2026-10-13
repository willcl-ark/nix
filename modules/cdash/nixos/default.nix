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
  secretFiles = lib.filterAttrs (_: value: value != null) {
    APP_KEY = cfg.appKeyFile;
    DB_PASSWORD = cfg.database.passwordFile;
    MAIL_PASSWORD = cfg.mail.passwordFile;
    AWS_ACCESS_KEY_ID = cfg.storage.accessKeyIdFile;
    AWS_SECRET_ACCESS_KEY = cfg.storage.secretAccessKeyFile;
    environment = cfg.environmentFile;
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
    appUrl = lib.mkOption {
      type = lib.types.str;
      default = "http://${cfg.hostName}";
      description = "Public URL of CDash.";
    };
    appKeyFile = lib.mkOption {
      type = lib.types.path;
      description = "Runtime file containing the persistent Laravel application key, for example a sops secret path.";
    };
    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Optional supplementary runtime dotenv file for settings without dedicated secret options.";
    };
    settings = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      description = "Nonsecret Laravel environment settings. These override the typed option defaults.";
    };
    database.createLocally = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Create the local cdash PostgreSQL database and role using peer authentication.";
    };
    database.host = lib.mkOption {
      type = lib.types.str;
      default = if cfg.database.createLocally then "/run/postgresql" else "localhost";
      description = "PostgreSQL host or Unix socket directory.";
    };
    database.port = lib.mkOption {
      type = lib.types.port;
      default = 5432;
      description = "PostgreSQL port.";
    };
    database.name = lib.mkOption {
      type = lib.types.str;
      default = "cdash";
      description = "PostgreSQL database name.";
    };
    database.user = lib.mkOption {
      type = lib.types.str;
      default = "cdash";
      description = "PostgreSQL role name.";
    };
    database.passwordFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Runtime file containing the PostgreSQL password.";
    };
    mail.mailer = lib.mkOption {
      type = lib.types.str;
      default = "sendmail";
      description = "Laravel mail transport, such as smtp, sendmail, or log.";
    };
    mail.fromAddress = lib.mkOption {
      type = lib.types.str;
      default = "cdash@localhost";
      description = "Sender email address.";
    };
    mail.replyAddress = lib.mkOption {
      type = lib.types.str;
      default = "cdash-noreply@localhost";
      description = "Reply-to email address.";
    };
    mail.fromName = lib.mkOption {
      type = lib.types.str;
      default = "CDash";
      description = "Sender display name.";
    };
    mail.host = lib.mkOption {
      type = lib.types.str;
      default = "smtp.mailgun.org";
      description = "SMTP server hostname.";
    };
    mail.port = lib.mkOption {
      type = lib.types.port;
      default = 587;
      description = "SMTP server port.";
    };
    mail.user = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "SMTP username.";
    };
    mail.encryption = lib.mkOption {
      type = lib.types.nullOr (
        lib.types.enum [
          "tls"
          "ssl"
        ]
      );
      default = "tls";
      description = "SMTP encryption, or null to disable transport encryption.";
    };
    mail.passwordFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Runtime file containing the SMTP password.";
    };
    submissionParsing = lib.mkOption {
      type = lib.types.enum [
        "sync"
        "database"
      ];
      default = "database";
      description = "Parse submissions synchronously or through the database queue.";
    };
    storage.driver = lib.mkOption {
      type = lib.types.enum [
        "local"
        "s3"
      ];
      default = "local";
      description = "Storage backend for submitted files.";
    };
    storage.bucket = lib.mkOption {
      type = lib.types.str;
      default = "cdash";
      description = "S3 bucket name.";
    };
    storage.region = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "S3 region, mapped to AWS_DEFAULT_REGION as required by CDash.";
    };
    storage.usePathStyleEndpoint = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Use path-style addressing for S3-compatible services.";
    };
    storage.endpoint = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Custom S3 service endpoint URL.";
    };
    storage.url = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Public S3 bucket URL.";
    };
    storage.accessKeyIdFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Runtime file containing the S3 access key ID.";
    };
    storage.secretAccessKeyFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Runtime file containing the S3 secret access key.";
    };
    backupTimeframe = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 48;
      description = "Hours to retain parsed submission files.";
    };
    defaultProject = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Project displayed when no project is specified.";
    };
    largeTextLimit = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 0;
      description = "Maximum bytes of build or test text before truncation; zero means unlimited.";
    };
    useVcsApi = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Allow CDash to query version control service APIs.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion =
          !cfg.database.createLocally || (cfg.database.name == "cdash" && cfg.database.user == "cdash");
        message = "services.cdash: local peer authentication requires database.name and database.user to be cdash.";
      }
    ];

    services.cdash.settings = lib.mapAttrs (_: lib.mkDefault) {
      APP_ENV = "production";
      APP_DEBUG = "false";
      GRAPHIQL_ENABLED = "false";
      APP_URL = cfg.appUrl;
      DB_HOST = cfg.database.host;
      DB_PORT = toString cfg.database.port;
      DB_DATABASE = cfg.database.name;
      DB_USERNAME = cfg.database.user;
      MAIL_MAILER = cfg.mail.mailer;
      MAIL_SENDMAIL_PATH = "/run/wrappers/bin/sendmail -bs -i";
      MAIL_FROM_ADDRESS = cfg.mail.fromAddress;
      MAIL_REPLY_ADDRESS = cfg.mail.replyAddress;
      MAIL_FROM_NAME = cfg.mail.fromName;
      MAIL_HOST = cfg.mail.host;
      MAIL_PORT = toString cfg.mail.port;
      MAIL_USERNAME = if cfg.mail.user == null then "null" else cfg.mail.user;
      MAIL_ENCRYPTION = if cfg.mail.encryption == null then "null" else cfg.mail.encryption;
      QUEUE_CONNECTION = cfg.submissionParsing;
      FILESYSTEM_DRIVER = cfg.storage.driver;
      AWS_BUCKET = cfg.storage.bucket;
      AWS_DEFAULT_REGION = if cfg.storage.region == null then "null" else cfg.storage.region;
      AWS_USE_PATH_STYLE_ENDPOINT = lib.boolToString cfg.storage.usePathStyleEndpoint;
      AWS_ENDPOINT = if cfg.storage.endpoint == null then "null" else cfg.storage.endpoint;
      AWS_URL = if cfg.storage.url == null then "null" else cfg.storage.url;
      BACKUP_TIMEFRAME = toString cfg.backupTimeframe;
      DEFAULT_PROJECT = if cfg.defaultProject == null then "null" else cfg.defaultProject;
      LARGE_TEXT_LIMIT = toString cfg.largeTextLimit;
      USE_VCS_API = lib.boolToString cfg.useVcsApi;
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
        LoadCredential = lib.mapAttrsToList (name: path: "${name}:${path}") secretFiles;
      };
      script = ''
        mkdir -p ${lib.escapeShellArg appRoot}/bootstrap/cache
        for entry in ${source}/*; do
          name=$(basename "$entry")
          case "$name" in storage|bootstrap) continue ;; esac
          ln -sfn "$entry" ${lib.escapeShellArg appRoot}/"$name"
        done
        ln -sfn ${source}/bootstrap/app.php ${lib.escapeShellArg appRoot}/bootstrap/app.php
        ${php} ${../scripts/write-environment.php} ${lib.escapeShellArg "${appRoot}/.env"}
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

    systemd.services.cdash-worker = lib.mkIf (cfg.settings.QUEUE_CONNECTION == "database") (
      lib.recursiveUpdate service {
        description = "CDash submission queue worker";
        wantedBy = [ "multi-user.target" ];
        serviceConfig = {
          ExecStart = "${artisan} queue:work --max-time=3600 --memory=256";
          Restart = "always";
          RestartSec = "5s";
        };
      }
    );
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
