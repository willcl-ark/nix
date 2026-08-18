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
  timeMachineFlagsText = lib.concatStringsSep "\n" builder.additionalTimeMachineFlags;

  submitSource = pkgs.writeText "guix-bitcoin-submit" (
    builtins.readFile ../scripts/guix-bitcoin-submit
  );
  nightlySource = pkgs.writeText "guix-bitcoin-nightly" (
    builtins.readFile ../scripts/guix-bitcoin-nightly
  );
  workerSource = pkgs.writeText "guix-manifest-worker" (
    builtins.readFile ../scripts/guix-manifest-worker
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
      export GUIX_BITCOIN_TIMEMACHINE_URL=${lib.escapeShellArg builder.timeMachineUrl}
      export GUIX_BITCOIN_TIMEMACHINE_COMMIT=${lib.escapeShellArg builder.timeMachineCommit}
      export GUIX_BITCOIN_TIMEMACHINE_FLAGS=${lib.escapeShellArg timeMachineFlagsText}
      exec ${pkgs.bash}/bin/bash ${source} "$@"
    '';
  submitTool = tool "guix-bitcoin-submit" submitSource;
  nightlyTool = tool "guix-bitcoin-nightly" nightlySource;
  workerTool = tool "guix-manifest-worker" workerSource;
  servicePath = [
    pkgs.bash
    pkgs.coreutils
    pkgs.curl
    pkgs.findutils
    pkgs.git
    pkgs.gnugrep
    pkgs.gnused
    config.services.guix.package
    submitTool
  ];
  jobDirectories = [
    "${cfg.dataDir}/bitcoin"
    jobsRoot
    "${jobsRoot}/.tmp"
    "${jobsRoot}/queued"
    "${jobsRoot}/running"
    "${jobsRoot}/succeeded"
    "${jobsRoot}/failed"
    profilesRoot
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
    enable = lib.mkEnableOption "Bitcoin Core Guix manifest builder";

    buildUser = lib.mkOption {
      type = lib.types.str;
      default = "guix-bitcoin-build";
      description = "System user that runs Bitcoin Core Guix manifest builds.";
    };

    buildGroup = lib.mkOption {
      type = lib.types.str;
      default = "guix-bitcoin-build";
      description = "System group that runs Bitcoin Core Guix manifest builds.";
    };

    bitcoinRepository = lib.mkOption {
      type = lib.types.str;
      default = "https://github.com/bitcoin/bitcoin";
      description = "Bitcoin Core Git repository used for nightly manifest submissions.";
    };

    bitcoinRemote = lib.mkOption {
      type = lib.types.str;
      default = "origin";
      description = "Remote name used for the Bitcoin Core checkout.";
    };

    bitcoinBranch = lib.mkOption {
      type = lib.types.str;
      default = "master";
      description = "Branch used for nightly manifest submissions.";
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

    additionalTimeMachineFlags = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Additional flags passed to guix time-machine.";
    };

    schedule = {
      onCalendar = lib.mkOption {
        type = lib.types.str;
        default = "*-*-* 06:00:00 UTC";
        description = "systemd OnCalendar value for nightly manifest submissions.";
      };

      randomizedDelaySec = lib.mkOption {
        type = lib.types.str;
        default = "0";
        description = "Randomized delay for nightly manifest submissions.";
      };
    };

    retention = {
      succeededJobMaxAgeDays = lib.mkOption {
        type = lib.types.ints.positive;
        default = 14;
        description = "Age in days after which succeeded manifest jobs are removed.";
      };

      failedJobMaxAgeDays = lib.mkOption {
        type = lib.types.ints.positive;
        default = 30;
        description = "Age in days after which failed manifest jobs are removed.";
      };

      profileMaxAgeDays = lib.mkOption {
        type = lib.types.ints.positive;
        default = 14;
        description = "Age in days after which materialized Guix profiles are removed.";
      };

      checkoutBuildMaxAgeDays = lib.mkOption {
        type = lib.types.ints.positive;
        default = 14;
        description = "Age in days after which guix-build-* checkout directories are removed.";
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
      description = "Submit the nightly Bitcoin Core Guix manifest job";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      path = servicePath;
      script = "${nightlyTool}/bin/guix-bitcoin-nightly";
      serviceConfig = {
        Type = "oneshot";
        User = builder.buildUser;
        Group = builder.buildGroup;
        ExecStartPre = prepareDirectories;
        ExecStartPost = "+${pkgs.systemd}/bin/systemctl start --no-block guix-manifest-worker.service";
      };
      environment = {
        GUIX_BITCOIN_REPOSITORY = builder.bitcoinRepository;
        GUIX_BITCOIN_REMOTE = builder.bitcoinRemote;
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

    systemd.services.guix-manifest-worker = {
      description = "Build queued immutable Guix manifest jobs";
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
      script = "${workerTool}/bin/guix-manifest-worker";
      serviceConfig = {
        Type = "oneshot";
        User = builder.buildUser;
        Group = builder.buildGroup;
        ExecStartPre = prepareDirectories;
      };
    };

    systemd.services.guix-bitcoin-build-cleanup = {
      description = "Clean old Bitcoin Core Guix manifest builder state";
      serviceConfig = {
        Type = "oneshot";
        User = builder.buildUser;
        Group = builder.buildGroup;
        ExecStartPre = prepareDirectories;
      };
      path = [
        pkgs.findutils
      ];
      script = ''
        find ${jobsRoot}/succeeded \
          -mindepth 1 -maxdepth 1 -type d \
          -mtime +${toString builder.retention.succeededJobMaxAgeDays} \
          -exec rm -rf {} +
        find ${jobsRoot}/failed \
          -mindepth 1 -maxdepth 1 -type d \
          -mtime +${toString builder.retention.failedJobMaxAgeDays} \
          -exec rm -rf {} +
        find ${profilesRoot} \
          -mindepth 2 -maxdepth 2 -type d \
          -mtime +${toString builder.retention.profileMaxAgeDays} \
          -exec rm -rf {} +
        find ${cfg.dataDir}/bitcoin \
          -maxdepth 1 -type d -name 'guix-build-*' \
          -mtime +${toString builder.retention.checkoutBuildMaxAgeDays} \
          -exec rm -rf {} +
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
