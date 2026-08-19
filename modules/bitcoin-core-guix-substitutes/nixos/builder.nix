{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.bitcoinCoreGuixSubstitutes;
  builder = cfg.builder;

  profilesRoot = "${cfg.dataDir}/profiles";
  jobsRoot = "${cfg.dataDir}/jobs";
  nativeSystemsText = lib.concatStringsSep " " builder.nativeSystems;
  targetHostsText = lib.concatStringsSep " " builder.targetHosts;
  publisherHostsText = lib.concatStringsSep " " builder.publisherHosts;
  runtimePath = lib.makeBinPath [
    pkgs.coreutils
    pkgs.curl
    pkgs.findutils
    pkgs.git
    pkgs.gnugrep
    pkgs.gnused
    pkgs.openssh
    pkgs.util-linux
    config.services.guix.package
  ];

  submitSource = pkgs.writeText "guix-bitcoin-submit" (
    builtins.readFile ../scripts/guix-bitcoin-submit
  );
  nightlySource = pkgs.writeText "guix-bitcoin-nightly" (
    builtins.readFile ../scripts/guix-bitcoin-nightly
  );
  workerSource = pkgs.writeText "guix-bitcoin-worker" (
    builtins.readFile ../scripts/guix-bitcoin-worker
  );
  publisherSource = pkgs.writeText "guix-bitcoin-publish" (
    builtins.readFile ../scripts/guix-bitcoin-publish
  );
  tool =
    name: source:
    pkgs.writeShellScriptBin name ''
      export GUIX_BITCOIN_DATA_DIR=${lib.escapeShellArg cfg.dataDir}
      export GUIX_BITCOIN_BUILD_USER=${lib.escapeShellArg builder.buildUser}
      export GUIX_BITCOIN_BUILD_GROUP=${lib.escapeShellArg builder.buildGroup}
      export GUIX_BITCOIN_BUILD_JOBS=${toString builder.buildJobs}
      export GUIX_BITCOIN_HOST_TRUE=${pkgs.coreutils}/bin/true
      export GUIX_BITCOIN_PREWARM_URL=http://${cfg.publishAddress}:${toString cfg.publishPort}
      export GUIX_BITCOIN_NATIVE_SYSTEMS=${lib.escapeShellArg nativeSystemsText}
      export GUIX_BITCOIN_TARGET_HOSTS=${lib.escapeShellArg targetHostsText}
      export GUIX_BITCOIN_PUBLISHER_HOSTS=${lib.escapeShellArg publisherHostsText}
      export GUIX_BITCOIN_TIMEMACHINE_URL=${lib.escapeShellArg builder.timeMachineUrl}
      export GUIX_BITCOIN_TIMEMACHINE_COMMIT=${lib.escapeShellArg builder.timeMachineCommit}
      export PATH=${lib.escapeShellArg runtimePath}:$PATH
      exec ${pkgs.bash}/bin/bash ${source} "$@"
    '';
  submitTool = tool "guix-bitcoin-submit" submitSource;
  nightlyTool = tool "guix-bitcoin-nightly" nightlySource;
  workerTool = tool "guix-bitcoin-worker" workerSource;
  publisherTool = tool "guix-bitcoin-publish" publisherSource;
  servicePath = [
    pkgs.bash
    pkgs.coreutils
    pkgs.curl
    pkgs.findutils
    pkgs.git
    pkgs.gnugrep
    pkgs.gnused
    pkgs.openssh
    pkgs.util-linux
    config.services.guix.package
    submitTool
  ];
  jobDirectories = [
    jobsRoot
    "${jobsRoot}/.tmp"
    "${jobsRoot}/queued"
    "${jobsRoot}/running"
    "${jobsRoot}/succeeded"
    "${jobsRoot}/failed"
    profilesRoot
    "${cfg.dataDir}/repositories"
    "${cfg.dataDir}/worktrees"
  ];
  prepareDirectories = [
    "+${pkgs.coreutils}/bin/install -d -m 0751 -o ${builder.buildUser} -g ${builder.buildGroup} ${cfg.dataDir}"
  ]
  ++ map (
    directory:
    "+${pkgs.coreutils}/bin/install -d -m 0750 -o ${builder.buildUser} -g ${builder.buildGroup} ${directory}"
  ) jobDirectories;
  bitcoinGuixHosts = [
    "x86_64-linux-gnu"
    "arm-linux-gnueabihf"
    "aarch64-linux-gnu"
    "riscv64-linux-gnu"
    "powerpc64-linux-gnu"
    "x86_64-w64-mingw32"
    "x86_64-apple-darwin"
    "arm64-apple-darwin"
  ];
in
{
  options.services.bitcoinCoreGuixSubstitutes.builder = {
    enable = lib.mkEnableOption "Bitcoin Core Guix source-job builder";

    buildUser = lib.mkOption {
      type = lib.types.str;
      default = "guix-bitcoin-build";
      description = "System user that runs Bitcoin Core Guix source jobs.";
    };

    buildGroup = lib.mkOption {
      type = lib.types.str;
      default = "guix-bitcoin-build";
      description = "System group that runs Bitcoin Core Guix source jobs.";
    };

    bitcoinRepository = lib.mkOption {
      type = lib.types.str;
      default = "https://github.com/bitcoin/bitcoin";
      description = "Bitcoin Core Git repository used for nightly source jobs.";
    };

    bitcoinBranch = lib.mkOption {
      type = lib.types.str;
      default = "master";
      description = "Branch used for nightly source jobs.";
    };

    buildJobs = lib.mkOption {
      type = lib.types.ints.positive;
      default = 16;
      description = "Core count passed to Guix build and time-machine commands.";
    };

    nativeSystems = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "x86_64-linux" ];
      description = "Native Guix systems whose manifest profiles are materialized.";
    };

    targetHosts = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = bitcoinGuixHosts;
      description = "Bitcoin Core HOST triplets built from each manifest.";
    };

    publisherHosts = lib.mkOption {
      type = lib.types.listOf (lib.types.strMatching "[A-Za-z0-9][A-Za-z0-9.-]*");
      default = [ ];
      description = "SSH hosts that receive completed job profile closures.";
    };

    timeMachineUrl = lib.mkOption {
      type = lib.types.str;
      default = "https://codeberg.org/guix/guix.git";
      description = "Guix repository URL used by guix time-machine.";
    };

    timeMachineCommit = lib.mkOption {
      type = lib.types.str;
      default = "c5eee3336cc1d10a3cc1c97fde2809c3451624d3";
      description = "Guix commit used by guix time-machine.";
    };

    schedule = {
      onCalendar = lib.mkOption {
        type = lib.types.str;
        default = "*-*-* 06:00:00 UTC";
        description = "systemd OnCalendar value for nightly source-job submissions.";
      };

      randomizedDelaySec = lib.mkOption {
        type = lib.types.str;
        default = "0";
        description = "Randomized delay for nightly source-job submissions.";
      };
    };

    retention = {
      succeededJobMaxAgeDays = lib.mkOption {
        type = lib.types.ints.positive;
        default = 14;
        description = "Age in days after which succeeded source jobs are removed.";
      };

      failedJobMaxAgeDays = lib.mkOption {
        type = lib.types.ints.positive;
        default = 30;
        description = "Age in days after which failed source jobs are removed.";
      };

      cleanupRandomizedDelaySec = lib.mkOption {
        type = lib.types.str;
        default = "1h";
        description = "Randomized delay for the builder cleanup timer.";
      };
    };
  };

  config = lib.mkIf builder.enable {
    assertions = [
      {
        assertion = cfg.enable;
        message = "services.bitcoinCoreGuixSubstitutes.builder.enable requires services.bitcoinCoreGuixSubstitutes.enable.";
      }
      {
        assertion = builder.nativeSystems != [ ];
        message = "services.bitcoinCoreGuixSubstitutes.builder.nativeSystems must not be empty.";
      }
      {
        assertion = builder.targetHosts != [ ];
        message = "services.bitcoinCoreGuixSubstitutes.builder.targetHosts must not be empty.";
      }
    ];

    users.users.${builder.buildUser} = {
      isSystemUser = true;
      group = builder.buildGroup;
      home = cfg.dataDir;
      createHome = true;
      homeMode = "0751";
    };
    users.groups.${builder.buildGroup} = { };

    environment.systemPackages = [
      submitTool
    ];

    systemd.tmpfiles.rules = [
      "z ${cfg.dataDir} 0751 ${builder.buildUser} ${builder.buildGroup} -"
    ]
    ++ map (
      directory: "d ${directory} 0750 ${builder.buildUser} ${builder.buildGroup} -"
    ) jobDirectories;

    systemd.services.guix-bitcoin-nightly = {
      description = "Submit the nightly Bitcoin Core Guix source job";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      path = servicePath;
      script = "${nightlyTool}/bin/guix-bitcoin-nightly";
      serviceConfig = {
        Type = "oneshot";
        User = builder.buildUser;
        Group = builder.buildGroup;
        ExecStartPre = prepareDirectories;
        ExecStartPost = "+${pkgs.systemd}/bin/systemctl start --no-block guix-bitcoin-worker.service";
      };
      environment = {
        GUIX_BITCOIN_REPOSITORY = builder.bitcoinRepository;
        GUIX_BITCOIN_BRANCH = builder.bitcoinBranch;
      };
    };

    systemd.timers.guix-bitcoin-nightly = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = builder.schedule.onCalendar;
        Persistent = true;
        RandomizedDelaySec = builder.schedule.randomizedDelaySec;
      };
    };

    systemd.services.guix-bitcoin-worker = {
      description = "Build queued Bitcoin Core Guix source jobs";
      after = [
        "network-online.target"
        "guix-daemon.service"
        "guix-publish.service"
      ];
      wants = [
        "network-online.target"
        "guix-daemon.service"
        "guix-publish.service"
      ];
      path = servicePath ++ [ workerTool ];
      script = "${workerTool}/bin/guix-bitcoin-worker";
      serviceConfig = {
        Type = "oneshot";
        User = builder.buildUser;
        Group = builder.buildGroup;
        ExecStartPre = prepareDirectories;
      }
      // lib.optionalAttrs (builder.publisherHosts != [ ]) {
        ExecStartPost = "+${publisherTool}/bin/guix-bitcoin-publish";
      };
    };

    systemd.services.guix-bitcoin-build-cleanup = {
      description = "Clean old Bitcoin Core Guix source builder state";
      serviceConfig = {
        Type = "oneshot";
        User = builder.buildUser;
        Group = builder.buildGroup;
        ExecStartPre = prepareDirectories;
      };
      path = [
        pkgs.bash
        pkgs.coreutils
        pkgs.findutils
        pkgs.git
        pkgs.gnugrep
        pkgs.util-linux
      ];
      script = ''
        set -euo pipefail

        cleanup_job() {
          local job=$1
          local job_id
          job_id=$(basename "$job")
          if ! [[ $job_id =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+-[0-9]+$ ]]; then
            echo "Skipping unsafe terminal job name: $job_id" >&2
            return 0
          fi

          if [[ -d ${cfg.dataDir}/repositories/bitcoin.git ]]; then
            (
              flock 9
              git --git-dir ${cfg.dataDir}/repositories/bitcoin.git worktree remove \
                --force ${cfg.dataDir}/worktrees/"$job_id" >/dev/null 2>&1 || true
              rm -rf -- ${cfg.dataDir}/worktrees/"$job_id"
              git --git-dir ${cfg.dataDir}/repositories/bitcoin.git worktree prune
              git --git-dir ${cfg.dataDir}/repositories/bitcoin.git update-ref \
                -d refs/guix-jobs/"$job_id" >/dev/null 2>&1 || true
            ) 9>${cfg.dataDir}/repositories/git.lock
          else
            rm -rf -- ${cfg.dataDir}/worktrees/"$job_id"
          fi
          rm -rf -- ${profilesRoot}/*/"$job_id"
          rm -rf -- "$job"
        }

        while IFS= read -r -d "" job; do
          cleanup_job "$job"
        done < <(find ${jobsRoot}/succeeded \
          -mindepth 1 -maxdepth 1 -type d \
          -mtime +${toString builder.retention.succeededJobMaxAgeDays} -print0)
        while IFS= read -r -d "" job; do
          cleanup_job "$job"
        done < <(find ${jobsRoot}/failed \
          -mindepth 1 -maxdepth 1 -type d \
          -mtime +${toString builder.retention.failedJobMaxAgeDays} -print0)
      '';
    };

    systemd.timers.guix-bitcoin-build-cleanup = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = "daily";
        Persistent = true;
        RandomizedDelaySec = builder.retention.cleanupRandomizedDelaySec;
      };
    };
  };
}
