{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.ralph;
  experimentCgroup = "/sys/fs/cgroup/system.slice/ralph-job@%i.service";
  jobLauncher = pkgs.writeScriptBin "ralph-job-launch" ''
    #!${pkgs.python3}/bin/python3
    import argparse
    import json
    import os
    from pathlib import Path
    import re

    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--instance", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[1-9][0-9]*-[0-9a-f]{32}", args.instance):
        parser.error("invalid job instance")
    manifest = args.state_dir / "attempts" / args.instance / "manifest.json"
    executable = json.loads(manifest.read_text())["executable"]
    if not re.fullmatch(r"/nix/store/[a-z0-9]{32}-[^/]+/bin/ralph-job", executable):
        parser.error("job executable must be a pinned Nix store package")
    os.execv(executable, [executable, "run", str(manifest)])
  '';
  defaultPackage = pkgs.callPackage ../../../pkgs/ralph-service {
    ralph = pkgs.callPackage ../../../pkgs/ralph { };
  };
  repositories = lib.filterAttrs (_: repo: repo.enable) cfg.repositories;
  registry = lib.mapAttrs (id: repo: {
    inherit (repo)
      origin
      repository
      strategy
      submodules
      ;
    forgejo_api = repo.forgejoApi;
    repository_url =
      if repo.repositoryUrl != null then
        repo.repositoryUrl
      else
        builtins.replaceStrings [ "/api/v1/repos/" ] [ "/" ] repo.forgejoApi;
    comment_marker =
      if repo.commentMarker != null then repo.commentMarker else "<!-- ralph:${repo.repository} -->";
    github_repository = repo.githubRepository;
    github_publication = repo.githubPublication;
    webhook_secret_file = repo.webhookSecretFile;
    forgejo_token_file = repo.forgejoTokenFile;
    github_token_file = repo.githubTokenFile;
    bot_login = repo.botLogin;
    max_workers = repo.workers;
    engine = repo.engine;
    engine_config =
      lib.optionalAttrs (repo.engine == "current") (
        {
          routing_mode = repo.routingMode;
          openai_api_base = cfg.openaiApiBase;
        }
        // lib.optionalAttrs (repo.agentsDir != null && !(repo.engineConfig ? prompts)) {
          agents_dir = toString repo.agentsDir;
        }
      )
      // repo.engineConfig;
    experiment_shell =
      let
        environment =
          if repo.writeVerifierEnvironment != null then
            repo.writeVerifierEnvironment
          else
            cfg.writeVerifierEnvironment;
      in
      if environment == null then null else lib.getExe environment;
  }) repositories;
  registryFile = (pkgs.formats.json { }).generate "ralph-repositories.json" registry;
  args = [
    "--listen"
    cfg.listenAddress
    "--port"
    (toString cfg.port)
    "--workers"
    (toString cfg.workers)
    "--state-dir"
    cfg.stateDir
    "--repositories"
    registryFile
    "--openai-key-file"
    cfg.openaiKeyFile
  ]
  ++ lib.optionals (cfg.reportDir != null && cfg.reportBaseUrl != null) [
    "--report-dir"
    cfg.reportDir
    "--report-base-url"
    cfg.reportBaseUrl
  ];
in
{
  options.services.ralph = {
    enable = lib.mkEnableOption "ralph pull request reviewer";

    repositories = lib.mkOption {
      default = { };
      description = "Repositories handled by the shared coordinator. Keys are stable repository IDs.";
      type = lib.types.attrsOf (
        lib.types.submodule {
          options = {
            enable = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = "Accept and process work for this repository.";
            };
            strategy = lib.mkOption {
              type = lib.types.enum [
                "core"
                "gui"
              ];
              default = "core";
              description = "Review strategy, independent of default/high compute settings.";
            };
            submodules = lib.mkOption {
              type = lib.types.attrsOf lib.types.str;
              default = { };
              description = "Allowed submodule paths mapped to trusted Git remotes; disabled by default.";
            };
            workers = lib.mkOption {
              type = lib.types.ints.positive;
              default = 1;
              description = "Concurrent review limit within the shared worker pool.";
            };
            writeVerifierEnvironment = lib.mkOption {
              type = lib.types.nullOr lib.types.package;
              default = null;
              description = "Repository-specific isolated experiment launcher; defaults to the global environment.";
            };
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

            githubPublication = lib.mkOption {
              type = lib.types.enum [
                "off"
                "major-only"
                "full"
              ];
              default = "off";
              description = "Publication destinations: off posts only to Forgejo; major-only also posts verified Critical and Major findings to GitHub; full posts the full review summary only to GitHub.";
            };

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

          };
        }
      );
    };

    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPackage;
      defaultText = lib.literalExpression "pkgs.callPackage ../../../pkgs/ralph-service { ralph = pkgs.callPackage ../../../pkgs/ralph { }; }";
      description = "ralph-service companion package to run.";
    };

    writeVerifierEnvironment = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      description = ''
        Build environment launcher for isolated Major-finding experiments.
        The flake module defaults to the pinned Bix environment. A replacement
        must set up its build environment and execute the command arguments.
        Direct module imports can supply this package or use the service
        package's environment.
      '';
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

    workers = lib.mkOption {
      type = lib.types.ints.positive;
      default = 3;
      description = "Maximum concurrent PR reviews. Reviews of the same PR run serially.";
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
      description = "Private directory containing Git objects, durable jobs, review traces and the usage ledger.";
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
    assertions =
      lib.concatLists (
        lib.mapAttrsToList (id: repo: [
          {
            assertion = builtins.match "[a-z][a-z0-9_-]*" id != null;
            message = "Ralph repository IDs must be lowercase stable identifiers.";
          }
          {
            assertion = (repo.githubTokenFile != null) == (repo.githubRepository != null);
            message = "Ralph repository ${id} requires githubTokenFile and githubRepository together.";
          }
          {
            assertion =
              repo.githubPublication == "off" || (repo.githubTokenFile != null && cfg.reportDir != null);
            message = "Ralph repository ${id} GitHub publication requires credentials and public reports.";
          }
        ]) repositories
      )
      ++ [
        {
          assertion = repositories != { };
          message = "services.ralph requires at least one enabled repository.";
        }
        {
          assertion =
            builtins.length (builtins.attrValues registry)
            == builtins.length (lib.unique (map (repo: repo.forgejo_api) (builtins.attrValues registry)));
          message = "Ralph repositories must have distinct Forgejo source identities.";
        }
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

    systemd.tmpfiles.rules = lib.optionals (cfg.reportDir != null) [
      "d ${cfg.reportDir} 0755 ${cfg.user} ${cfg.group} -"
      "d ${cfg.stateDir}/stats 0700 ${cfg.user} ${cfg.group} -"
    ];

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
              "--repositories"
              registryFile
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
        ReadWritePaths = [
          cfg.reportDir
          "${cfg.stateDir}/stats"
        ];
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
              "--repositories"
              registryFile
              "--openai-key-file"
              cfg.openaiKeyFile
              "--openai-api-base"
              cfg.openaiApiBase
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
            "--repositories"
            registryFile
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
          "-${cfg.stateDir}/usage.sqlite3"
          "-${cfg.stateDir}/followup"
        ];
        NoNewPrivileges = true;
        PrivateTmp = true;
      };
    };

    # Only fixed unprivileged worker instances may be managed by the coordinator.
    security.polkit.enable = true;
    security.polkit.extraConfig = ''
      polkit.addRule(function(action, subject) {
        if (subject.user === ${builtins.toJSON cfg.user} &&
            action.id === "org.freedesktop.systemd1.manage-units" &&
            /^ralph-job@[1-9][0-9]*-[0-9a-f]{32}\.service$/.test(action.lookup("unit")) &&
            ["start", "stop"].indexOf(action.lookup("verb")) !== -1) {
          return polkit.Result.YES;
        }
      });
    '';

    systemd.services.ralph = {
      description = "ralph pull request coordinator";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      environment.RALPH_JOB_EXECUTABLE = "${cfg.package}/bin/ralph-job";
      serviceConfig = {
        ExecStart = "${lib.getExe' cfg.package "ralph-service"} ${lib.escapeShellArgs args}";
        User = cfg.user;
        Group = cfg.group;
        StateDirectory = lib.removePrefix "/var/lib/" cfg.stateDir;
        StateDirectoryMode = "0700";
        WorkingDirectory = cfg.stateDir;
        Restart = "always";
        RestartSec = "10s";
        # Independent job units survive coordinator replacement.
        TimeoutStopSec = "10s";
        KillMode = "control-group";
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

    systemd.services."ralph-job@" = {
      description = "ralph review attempt %i";
      # Existing attempts retain their process, package and sandbox until exit.
      restartIfChanged = false;
      stopIfChanged = false;
      environment = {
        RALPH_WRITE_VERIFIER_CGROUP = experimentCgroup;
        RALPH_WRITE_VERIFIER_CCACHE = "${cfg.stateDir}/ccache";
        RALPH_WRITE_VERIFIER_LOCK = "${cfg.stateDir}/experiment.lock";
      };
      serviceConfig = {
        Type = "exec";
        ExecStart = "${jobLauncher}/bin/ralph-job-launch --state-dir ${lib.escapeShellArg cfg.stateDir} --instance %i";
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.stateDir;
        # Template instances otherwise get their own implicit slice.
        Slice = "system.slice";
        Restart = "no";
        KillMode = "control-group";
        TimeoutStopSec = "10s";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [
          cfg.stateDir
          experimentCgroup
        ]
        ++ lib.optional (cfg.reportDir != null) cfg.reportDir;
        ReadOnlyPaths = [ "/sys" ];
        PrivateTmp = true;
        PrivateDevices = true;
        # Masking /proc children prevents Bubblewrap from mounting a fresh
        # /proc inside its unprivileged user namespace.
        ProtectKernelTunables = false;
        ProtectKernelModules = true;
        # Only the delegated subtree is writable. Keep the review controller
        # outside each experiment's memory, process and CPU limits.
        ProtectControlGroups = false;
        Delegate = [
          "cpu"
          "memory"
          "pids"
        ];
        DelegateSubgroup = "controller";
        OOMPolicy = "continue";
        RestrictSUIDSGID = true;
        # Bubblewrap isolates experiments with an unprivileged user namespace.
        RestrictNamespaces = [
          "user"
          "mnt"
          "pid"
          "net"
          "ipc"
          "uts"
        ];
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        RestrictRealtime = true;
      };
    };
  };
}
