{
  config,
  lib,
  options,
  pkgs,
  ...
}:

let
  cfg = config.services.ralph;
  defaultPackage = pkgs.callPackage ../../../pkgs/ralph-service {
    ralph = pkgs.callPackage ../../../pkgs/ralph { };
  };
  currentEngine = cfg.engine == "current";
  openaiApiBase = cfg.engineConfig.openai_api_base or cfg.openaiApiBase;
  currentEngineConfigDefaults = lib.optionalAttrs currentEngine (
    {
      routing_mode = cfg.routingMode;
      openai_api_base = cfg.openaiApiBase;
    }
    // lib.optionalAttrs (!(cfg.engineConfig ? prompts) && cfg.agentsDir != null) {
      agents_dir = toString cfg.agentsDir;
    }
  );
  engineConfigFile = (pkgs.formats.json { }).generate "ralph-engine-config.json" (
    currentEngineConfigDefaults // cfg.engineConfig
  );
  currentOptionsExplicit =
    options.services.ralph.agentsDir.highestPrio < 1500
    || options.services.ralph.routingMode.highestPrio < 1500;
  defaultMarker = "<!-- ralph:${cfg.repository} -->";
  commentMarker = if cfg.commentMarker == null then defaultMarker else cfg.commentMarker;
  repositoryUrl =
    if cfg.repositoryUrl != null then
      cfg.repositoryUrl
    else
      builtins.replaceStrings [ "/api/v1/repos/" ] [ "/" ] cfg.forgejoApi;
  args = [
    "--listen"
    cfg.listenAddress
    "--port"
    (toString cfg.port)
    "--workers"
    (toString cfg.workers)
    "--state-dir"
    cfg.stateDir
    "--origin"
    cfg.origin
    "--repository"
    cfg.repository
    "--forgejo-api"
    cfg.forgejoApi
    "--comment-marker"
    commentMarker
    "--engine"
    cfg.engine
    "--engine-config"
    engineConfigFile
    "--review-budget-usd"
    (toString cfg.reviewBudgetUsd)
    "--openai-key-file"
    cfg.openaiKeyFile
    "--webhook-secret-file"
    cfg.webhookSecretFile
    "--forgejo-token-file"
    cfg.forgejoTokenFile
    "--bot-login"
    cfg.botLogin
  ]
  ++ lib.optionals (cfg.githubTokenFile != null && cfg.githubRepository != null) [
    "--github-token-file"
    cfg.githubTokenFile
    "--github-repository"
    cfg.githubRepository
  ]
  ++ lib.optionals cfg.githubPostMajorFindings [ "--github-post-major-findings" ]
  ++ lib.optionals (cfg.repositoryUrl != null) [
    "--repository-url"
    cfg.repositoryUrl
  ]
  ++ lib.optionals (cfg.reportDir != null && cfg.reportBaseUrl != null) [
    "--report-dir"
    cfg.reportDir
    "--report-base-url"
    cfg.reportBaseUrl
  ]
  ++ lib.optionals (cfg.monthlyBudgetUsd != null) [
    "--monthly-budget-usd"
    (toString cfg.monthlyBudgetUsd)
  ];
in
{
  options.services.ralph = {
    enable = lib.mkEnableOption "ralph pull request reviewer";

    engine = lib.mkOption {
      type = lib.types.str;
      default = "current";
      description = "Trusted local Ralph engine module to run. The default selects the current review pipeline.";
    };

    engineConfig = lib.mkOption {
      type = lib.types.attrsOf lib.types.anything;
      default = { };
      description = ''
        JSON-serializable configuration object passed to the selected engine.
        For the current engine, this can pin resolved prompts and model settings.
      '';
    };

    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPackage;
      defaultText = lib.literalExpression "pkgs.callPackage ../../../pkgs/ralph-service { ralph = pkgs.callPackage ../../../pkgs/ralph { }; }";
      description = "ralph-service companion package to run.";
    };

    origin = lib.mkOption {
      type = lib.types.str;
      description = "Git remote URL used to fetch base branches and pull request heads.";
    };

    repository = lib.mkOption {
      type = lib.types.str;
      example = "owner/repo";
      description = "Forgejo repository full name accepted from webhook payloads.";
    };

    forgejoApi = lib.mkOption {
      type = lib.types.str;
      example = "https://git.example.org/api/v1/repos/owner/repo";
      description = "Forgejo repository API URL.";
    };

    githubPostMajorFindings = lib.mkEnableOption "posting verified Major findings to GitHub";

    githubTokenFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Runtime GitHub token file for authenticated upstream reads and optional posting.";
    };

    githubRepository = lib.mkOption {
      type = lib.types.nullOr (lib.types.strMatching "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+");
      default = null;
      description = "Upstream GitHub owner/repo. PR numbers must match the Forgejo mirror.";
    };

    telegram = {
      enable = lib.mkEnableOption "Telegram review publication and monitoring";

      botTokenFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Runtime file containing the Telegram bot token. Keep the token out of the Nix store.";
      };

      chatId = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "@ralph_reviews";
        description = "Telegram channel username prefixed with @, or numeric chat ID as a string.";
      };
    };

    repositoryUrl = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "https://git.example.org/owner/repo";
      description = "Expected repository HTML URL in webhook payloads. Unset derives it from forgejoApi.";
    };

    commentMarker = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      defaultText = lib.literalExpression "\"<!-- ralph:\${config.services.ralph.repository} -->\"";
      description = "Hidden marker used to find and update ralph's existing comment.";
    };

    agentsDir = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Optional directory of review agent settings and prompts. Unset uses the core package defaults.";
    };

    workers = lib.mkOption {
      type = lib.types.ints.positive;
      default = 3;
      description = "Maximum concurrent PR reviews. Reviews of the same PR run serially.";
    };

    reviewBudgetUsd = lib.mkOption {
      type = lib.types.addCheck lib.types.number (value: value > 0);
      default = 5.00;
      description = ''
        Base per-review spending ceiling in USD. The high profile uses five
        times this allowance. Each API request reserves a
        conservative estimate before starting. Reviews that exhaust their
        allowance report incomplete coverage.
        Reservations are estimates, not an invoice cap.
      '';
    };

    followupBudgetUsd = lib.mkOption {
      type = lib.types.addCheck lib.types.number (value: value > 0);
      default = 0.10;
      description = ''
        Separate per-update spending ceiling in USD for background assessments
        of previously published findings. Uses its own ledger and never consumes
        the main review's per-review or monthly allowance.
      '';
    };

    followupPromptFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Optional addressed-finding prompt override. Unset uses the companion package default.";
    };

    followupModel = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Optional addressed-finding model override. Independent of the main review agents.";
    };

    monthlyBudgetUsd = lib.mkOption {
      type = lib.types.nullOr (lib.types.addCheck lib.types.number (value: value > 0));
      default = null;
      description = "Optional monthly API allowance in USD, shared by reviews in this state directory.";
    };

    subscriptionUsage = {
      enable = lib.mkEnableOption "passive Codex and Claude subscription quota reporting";

      managementUrl = lib.mkOption {
        type = lib.types.str;
        default = "http://127.0.0.1:8317";
        description = "CLIProxyAPI v8 management server URL, without embedded credentials.";
      };

      managementKeyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = ''
          Runtime file containing the separate CLIProxyAPI management key.
          Loaded only into the stats service through a systemd credential.
          Public reports contain sanitized quotas, never account identities or keys.
        '';
      };
    };

    routingMode = lib.mkOption {
      type = lib.types.enum [
        "enabled"
        "shadow"
        "full"
      ];
      default = "enabled";
      description = ''
        Select audits with conservative Luna routing, record routing decisions
        while running the full review in shadow mode, or always run the full
        review. All modes respect the review allowance.
      '';
    };

    listenAddress = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Address for the webhook and review badge HTTP server.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8765;
      description = "Port for the webhook and review badge HTTP server.";
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/ralph";
      description = "Private directory containing Git objects, durable jobs, review traces and the spend ledger.";
    };

    reportDir = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Public directory for full HTML and JSON review reports. Serve it with a web server; keep it separate from stateDir.";
    };

    reportBaseUrl = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Public HTTP URL serving reportDir. Enables full report links in review comments.";
    };

    openaiKeyFile = lib.mkOption {
      type = lib.types.str;
      description = "Path to a file containing the OpenAI API key or the configured proxy's access key.";
    };

    openaiApiBase = lib.mkOption {
      type = lib.types.str;
      default = "https://api.openai.com/v1";
      example = "http://127.0.0.1:8317/v1";
      description = "OpenAI-compatible Responses API base URL for the current engine, including Claude adversarial review, and follow-up assessments.";
    };

    webhookSecretFile = lib.mkOption {
      type = lib.types.str;
      description = "Path to a file containing the Forgejo webhook secret.";
    };

    forgejoTokenFile = lib.mkOption {
      type = lib.types.str;
      description = "Path to a file containing the Forgejo API token.";
    };

    botLogin = lib.mkOption {
      type = lib.types.str;
      description = "Forgejo account login that owns the review comment.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "ralph";
      description = "System user running ralph.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "ralph";
      description = "System group running ralph.";
    };

  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion =
          !cfg.subscriptionUsage.enable
          || (
            cfg.subscriptionUsage.managementKeyFile != null
            && cfg.reportDir != null
            && cfg.reportBaseUrl != null
          );
        message = "services.ralph.subscriptionUsage requires a managementKeyFile and public report configuration.";
      }
      {
        assertion = (cfg.githubTokenFile != null) == (cfg.githubRepository != null);
        message = "services.ralph.githubTokenFile and githubRepository must be set together.";
      }
      {
        assertion =
          !cfg.githubPostMajorFindings || (cfg.githubTokenFile != null && cfg.githubRepository != null);
        message = "services.ralph.githubPostMajorFindings requires githubTokenFile and githubRepository.";
      }
      {
        assertion = !cfg.githubPostMajorFindings || (cfg.reportDir != null && cfg.reportBaseUrl != null);
        message = "services.ralph GitHub publication requires reportDir and reportBaseUrl.";
      }
      {
        assertion =
          !cfg.telegram.enable || (cfg.telegram.botTokenFile != null && cfg.telegram.chatId != null);
        message = "services.ralph.telegram.enable requires telegram.botTokenFile and telegram.chatId.";
      }
      {
        assertion = !cfg.telegram.enable || (cfg.reportDir != null && cfg.reportBaseUrl != null);
        message = "services.ralph Telegram publication requires reportDir and reportBaseUrl.";
      }
      {
        assertion = lib.hasPrefix "/var/lib/" cfg.stateDir;
        message = "services.ralph.stateDir must be under /var/lib so systemd can manage it with StateDirectory.";
      }
      {
        assertion = (cfg.reportDir == null) == (cfg.reportBaseUrl == null);
        message = "services.ralph.reportDir and reportBaseUrl must be set together.";
      }
      {
        assertion = currentEngine || !currentOptionsExplicit;
        message = "services.ralph.agentsDir and routingMode are current-engine options.";
      }
      {
        assertion =
          cfg.reportDir == null
          || (
            lib.hasPrefix "/" cfg.reportDir
            && (
              let
                reportDir = toString (builtins.toPath cfg.reportDir);
                stateDir = toString (builtins.toPath cfg.stateDir);
              in
              reportDir != "/"
              && reportDir != stateDir
              && !(lib.hasPrefix "${stateDir}/" reportDir)
              && !(lib.hasPrefix "${reportDir}/" stateDir)
            )
          );
        message = "services.ralph.reportDir must be absolute and separate from the private stateDir.";
      }
    ];

    systemd.tmpfiles.rules = lib.optional (
      cfg.reportDir != null
    ) "d ${cfg.reportDir} 0755 ${cfg.user} ${cfg.group} -";

    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.stateDir;
    };
    users.groups.${cfg.group} = { };

    systemd.services.ralph-stats = lib.mkIf (cfg.reportDir != null && cfg.reportBaseUrl != null) {
      description = "Publish ralph review statistics";
      after = [ "ralph.service" ];
      requires = [ "ralph.service" ];
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        ExecStart = "${cfg.package}/bin/ralph-evaluate ${
          lib.escapeShellArgs (
            [
              "stats"
              "--state-dir"
              cfg.stateDir
              "--report-dir"
              cfg.reportDir
              "--repository-url"
              repositoryUrl
              "--report-base-url"
              cfg.reportBaseUrl
            ]
            ++ lib.optionals cfg.subscriptionUsage.enable [
              "--proxy-management-url"
              cfg.subscriptionUsage.managementUrl
              "--proxy-management-key-file"
              "%d/proxy-management-key"
            ]
          )
        }";
        LoadCredential = lib.optionals cfg.subscriptionUsage.enable [
          "proxy-management-key:${cfg.subscriptionUsage.managementKeyFile}"
        ];
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ cfg.reportDir ];
        NoNewPrivileges = true;
        PrivateTmp = true;
      };
    };

    systemd.timers.ralph-stats = lib.mkIf (cfg.reportDir != null && cfg.reportBaseUrl != null) {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "1m";
        OnUnitActiveSec = "5m";
      };
    };

    systemd.services.ralph-followup = lib.mkIf (cfg.reportDir != null && cfg.reportBaseUrl != null) {
      description = "Assess changes addressing published Ralph findings";
      after = [
        "network-online.target"
        "ralph.service"
      ];
      wants = [ "network-online.target" ];
      requires = [ "ralph.service" ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${cfg.package}/bin/ralph-followup ${
          lib.escapeShellArgs (
            [
              "--state-dir"
              cfg.stateDir
              "--origin"
              cfg.origin
              "--openai-key-file"
              cfg.openaiKeyFile
              "--openai-api-base"
              openaiApiBase
              "--budget-usd"
              (toString cfg.followupBudgetUsd)
            ]
            ++ lib.optionals (cfg.followupPromptFile != null) [
              "--prompt-file"
              cfg.followupPromptFile
            ]
            ++ lib.optionals (cfg.followupModel != null) [
              "--model"
              cfg.followupModel
            ]
          )
        }";
        User = cfg.user;
        Group = cfg.group;
        StateDirectory = "${lib.removePrefix "/var/lib/" cfg.stateDir}/followup";
        StateDirectoryMode = "0700";
        WorkingDirectory = "${cfg.stateDir}/followup";
        ReadOnlyPaths = [ cfg.stateDir ];
        ReadWritePaths = [ "${cfg.stateDir}/followup" ];
        ProtectSystem = "strict";
        ProtectHome = true;
        NoNewPrivileges = true;
        PrivateTmp = true;
        Nice = 10;
        IOSchedulingClass = "idle";
        TimeoutStartSec = "30m";
      };
    };

    systemd.timers.ralph-followup = lib.mkIf (cfg.reportDir != null && cfg.reportBaseUrl != null) {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "2m";
        OnUnitInactiveSec = "5m";
      };
    };

    systemd.services.ralph-telegram = lib.mkIf cfg.telegram.enable {
      description = "Publish Ralph reviews and monitor service status on Telegram";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        ExecStart = "${lib.getExe' cfg.package "ralph-telegram"} ${
          lib.escapeShellArgs [
            "--state-dir"
            cfg.stateDir
            "--report-dir"
            cfg.reportDir
            "--report-base-url"
            cfg.reportBaseUrl
            "--repository-url"
            repositoryUrl
            "--telegram-token-file"
            cfg.telegram.botTokenFile
            "--telegram-chat-id"
            cfg.telegram.chatId
          ]
        }";
        User = cfg.user;
        Group = cfg.group;
        StateDirectory = lib.removePrefix "/var/lib/" cfg.stateDir;
        StateDirectoryMode = "0700";
        WorkingDirectory = cfg.stateDir;
        Restart = "always";
        RestartSec = "10s";
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [
          cfg.stateDir
          cfg.reportDir
        ];
        ReadOnlyPaths = [
          "-${cfg.stateDir}/jobs.sqlite3"
          "-${cfg.stateDir}/spend.sqlite3"
          "-${cfg.stateDir}/ppq-spend.sqlite3"
          "-${cfg.stateDir}/followup"
        ];
        NoNewPrivileges = true;
        PrivateTmp = true;
      };
    };

    systemd.services.ralph = {
      description = "ralph pull request reviewer";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      preStart = ''
        ${lib.getExe' cfg.package "ralph-service"} --state-dir ${lib.escapeShellArg cfg.stateDir} --migrate-state
      '';

      serviceConfig = {
        ExecStart = "${lib.getExe' cfg.package "ralph-service"} ${lib.escapeShellArgs args}";
        User = cfg.user;
        Group = cfg.group;
        StateDirectory = lib.removePrefix "/var/lib/" cfg.stateDir;
        StateDirectoryMode = "0700";
        WorkingDirectory = cfg.stateDir;

        Restart = "always";
        RestartSec = "10s";

        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ cfg.stateDir ] ++ lib.optional (cfg.reportDir != null) cfg.reportDir;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        RestrictNamespaces = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        RestrictRealtime = true;
      };
    };
  };
}
