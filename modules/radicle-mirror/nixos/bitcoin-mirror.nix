{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.radicleMirror;
  mirror = cfg.bitcoinMirror;
  rad = "${config.services.radicle.package}/bin/rad";

  radSystem = pkgs.writeShellScript "rad-system" ''
    set -o allexport
    HOME=/var/lib/radicle
    RAD_HOME=/var/lib/radicle
    exec ${pkgs.util-linux}/bin/nsenter -a \
      -t "$(${config.systemd.package}/bin/systemctl show -P MainPID radicle-node.service)" \
      -S "$(${config.systemd.package}/bin/systemctl show -P UID radicle-node.service)" \
      -G "$(${config.systemd.package}/bin/systemctl show -P GID radicle-node.service)" \
      ${rad} "$@"
  '';

  mirrorEnv = {
    HOME = mirror.dataDir;
    RAD_HOME = mirror.dataDir;
  };

  mirrorInit = pkgs.writeShellScript "radicle-mirror-init" ''
    set -euo pipefail

    if [[ ! -f "$RAD_HOME/config.json" ]]; then
      printf '\n' | ${rad} auth \
        --alias ${lib.escapeShellArg mirror.alias} \
        --stdin
    fi
  '';

  delegateArgs = lib.concatMapStringsSep " " (
    did: "--delegate ${lib.escapeShellArg did}"
  ) mirror.delegateDids;

  mirrorClone = pkgs.writeShellScript "radicle-mirror-clone" ''
    set -euo pipefail

    repo="$RAD_HOME/${mirror.checkoutDirectory}"
    rid_file="$RAD_HOME/${mirror.ridFile}"

    if [[ ! -d "$repo/.git" ]]; then
      ${pkgs.git}/bin/git clone ${lib.escapeShellArg mirror.upstreamUrl} "$repo"
      cd "$repo"
      ${rad} init \
        --name ${lib.escapeShellArg mirror.repositoryName} \
        --description ${lib.escapeShellArg mirror.repositoryDescription} \
        --default-branch ${lib.escapeShellArg mirror.defaultBranch} \
        --scope followed \
        --public \
        --set-upstream \
        --no-confirm
      rid="$(${rad} .)"
      printf '%s\n' "$rid" > "$rid_file"
      ${lib.optionalString (mirror.delegateDids != [ ]) ''
        ${rad} id update \
          --title ${lib.escapeShellArg "Add configured mirror delegates"} \
          ${delegateArgs} \
          --threshold ${toString mirror.delegateThreshold} \
          --no-confirm
      ''}
    elif [[ ! -f "$rid_file" ]]; then
      cd "$repo"
      ${rad} . > "$rid_file"
    fi
  '';

  mirrorUpdate = pkgs.writeShellScript "radicle-mirror-update" ''
    set -euo pipefail

    repo="$RAD_HOME/${mirror.checkoutDirectory}"

    cd "$repo"
    if ${pkgs.git}/bin/git remote get-url origin >/dev/null 2>&1; then
      ${pkgs.git}/bin/git remote set-url origin ${lib.escapeShellArg mirror.upstreamUrl}
    else
      ${pkgs.git}/bin/git remote add origin ${lib.escapeShellArg mirror.upstreamUrl}
    fi
    ${pkgs.git}/bin/git fetch --prune --tags origin '+refs/heads/*:refs/remotes/origin/*'
    ${pkgs.git}/bin/git checkout -B ${lib.escapeShellArg mirror.defaultBranch} origin/${lib.escapeShellArg mirror.defaultBranch}
    ${pkgs.git}/bin/git push rad ${lib.escapeShellArg mirror.defaultBranch} -o sync
  '';

  mirrorSeed = pkgs.writeShellScript "radicle-mirror-seed" ''
    set -euo pipefail

    rid="$(cat ${lib.escapeShellArg "${mirror.dataDir}/${mirror.ridFile}"})"
    ${radSystem} seed "$rid"
  '';

  mirrorSync = pkgs.writeShellScript "radicle-mirror-sync" ''
    set -euo pipefail

    rid="$(cat "$RAD_HOME/${mirror.ridFile}")"
    ${rad} node connect ${cfg.seed.nodeId}@${mirror.seedAddress} || true
    ${rad} sync \
      --announce \
      --seed ${cfg.seed.nodeId} \
      --replicas 1 \
      --timeout ${mirror.syncTimeout} \
      "$rid"
  '';
in
{
  options.services.radicleMirror.bitcoinMirror = {
    enable = lib.mkEnableOption "bitcoin/bitcoin Radicle mirror";

    user = lib.mkOption {
      type = lib.types.str;
      default = "radicle-mirror";
      description = "System user that owns the mirror identity and checkout.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "radicle-mirror";
      description = "System group that owns the mirror identity and checkout.";
    };

    dataDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/radicle-mirror";
      description = "Data directory for the mirror identity and checkout.";
    };

    alias = lib.mkOption {
      type = lib.types.str;
      default = "bitcoin-core-mirror";
      description = "Radicle alias for the mirror identity.";
    };

    upstreamUrl = lib.mkOption {
      type = lib.types.str;
      default = "https://github.com/bitcoin/bitcoin.git";
      description = "Git upstream mirrored into Radicle.";
    };

    repositoryName = lib.mkOption {
      type = lib.types.str;
      default = "bitcoin";
      description = "Radicle repository name.";
    };

    repositoryDescription = lib.mkOption {
      type = lib.types.str;
      default = "Bitcoin Core GitHub mirror";
      description = "Radicle repository description.";
    };

    defaultBranch = lib.mkOption {
      type = lib.types.str;
      default = "master";
      description = "Branch mirrored from upstream.";
    };

    checkoutDirectory = lib.mkOption {
      type = lib.types.str;
      default = "bitcoin";
      description = "Directory name under dataDir for the Git checkout.";
    };

    ridFile = lib.mkOption {
      type = lib.types.str;
      default = "bitcoin.rid";
      description = "File under dataDir where the generated RID is stored.";
    };

    delegateDids = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Additional Radicle DIDs to add as repository delegates on first initialization.";
    };

    delegateThreshold = lib.mkOption {
      type = lib.types.ints.positive;
      default = 1;
      description = "Delegate threshold used when adding configured delegates.";
    };

    nodeListenAddress = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Address the mirror node listens on.";
    };

    nodeListenPort = lib.mkOption {
      type = lib.types.port;
      default = 8777;
      description = "Port the mirror node listens on.";
    };

    seedAddress = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1:8776";
      description = "Address used by the mirror node to connect to the public seed.";
    };

    syncTimeout = lib.mkOption {
      type = lib.types.str;
      default = "600s";
      description = "Timeout for announcing mirror refs to the public seed.";
    };

    timer = {
      onBootSec = lib.mkOption {
        type = lib.types.str;
        default = "10min";
        description = "Delay before the first mirror run after boot.";
      };

      onUnitActiveSec = lib.mkOption {
        type = lib.types.str;
        default = "1h";
        description = "Delay between mirror runs.";
      };
    };
  };

  config = lib.mkIf (cfg.enable && mirror.enable) {
    assertions = [
      {
        assertion = cfg.seed.enable;
        message = "services.radicleMirror.seed.enable must be true when bitcoinMirror is enabled.";
      }
      {
        assertion = cfg.seed.nodeId != null;
        message = "services.radicleMirror.seed.nodeId must be set when bitcoinMirror is enabled.";
      }
    ];

    users.users.${mirror.user} = {
      description = "Radicle Bitcoin Core mirror";
      group = mirror.group;
      home = mirror.dataDir;
      isSystemUser = true;
    };
    users.groups.${mirror.group} = { };

    systemd.tmpfiles.rules = [
      "d ${mirror.dataDir} 0750 ${mirror.user} ${mirror.group} -"
    ];

    systemd.services.radicle-mirror-init = {
      description = "Initialize the Radicle Bitcoin Core mirror identity";
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        Type = "oneshot";
        User = mirror.user;
        Group = mirror.group;
        WorkingDirectory = mirror.dataDir;
        ExecStart = mirrorInit;
      };
      environment = mirrorEnv;
    };

    systemd.services.radicle-mirror-node = {
      description = "Radicle Bitcoin Core mirror node";
      after = [
        "network-online.target"
        "radicle-mirror-init.service"
      ];
      requires = [ "radicle-mirror-init.service" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        ExecStart = "${config.services.radicle.package}/bin/radicle-node --force --listen ${mirror.nodeListenAddress}:${toString mirror.nodeListenPort}";
        Restart = "on-failure";
        RestartSec = "30";
        User = mirror.user;
        Group = mirror.group;
        WorkingDirectory = mirror.dataDir;
      };
      environment = mirrorEnv;
    };

    systemd.services.radicle-mirror-clone = {
      description = "Clone bitcoin/bitcoin and initialize its Radicle mirror";
      after = [
        "network-online.target"
        "radicle-mirror-node.service"
      ];
      requires = [ "radicle-mirror-node.service" ];
      wants = [ "network-online.target" ];
      path = [
        pkgs.git
        config.services.radicle.package
      ];
      serviceConfig = {
        Type = "oneshot";
        User = mirror.user;
        Group = mirror.group;
        WorkingDirectory = mirror.dataDir;
        ExecStart = mirrorClone;
      };
      environment = mirrorEnv;
    };

    systemd.services.radicle-mirror-update = {
      description = "Update the Bitcoin Core Git mirror";
      after = [
        "network-online.target"
        "radicle-mirror-clone.service"
      ];
      requires = [ "radicle-mirror-clone.service" ];
      wants = [ "network-online.target" ];
      path = [
        pkgs.git
        config.services.radicle.package
      ];
      serviceConfig = {
        Type = "oneshot";
        User = mirror.user;
        Group = mirror.group;
        WorkingDirectory = mirror.dataDir;
        ExecStart = mirrorUpdate;
      };
      environment = mirrorEnv;
    };

    systemd.services.radicle-mirror-seed = {
      description = "Allow the Bitcoin Core mirror RID on the public Radicle seed";
      after = [
        "radicle-node.service"
        "radicle-mirror-clone.service"
      ];
      requires = [
        "radicle-node.service"
        "radicle-mirror-clone.service"
      ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = mirrorSeed;
      };
    };

    systemd.services.radicle-mirror-sync = {
      description = "Announce the Bitcoin Core Radicle mirror to the public seed";
      after = [
        "radicle-mirror-node.service"
        "radicle-mirror-update.service"
        "radicle-mirror-seed.service"
      ];
      requires = [
        "radicle-mirror-node.service"
        "radicle-mirror-update.service"
        "radicle-mirror-seed.service"
      ];
      serviceConfig = {
        Type = "oneshot";
        User = mirror.user;
        Group = mirror.group;
        WorkingDirectory = mirror.dataDir;
        ExecStart = mirrorSync;
      };
      environment = mirrorEnv;
    };

    systemd.services.radicle-mirror-bitcoin-core = {
      description = "Mirror bitcoin/bitcoin into Radicle";
      after = [ "radicle-mirror-sync.service" ];
      requires = [ "radicle-mirror-sync.service" ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${pkgs.coreutils}/bin/true";
      };
    };

    systemd.timers.radicle-mirror-bitcoin-core = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = mirror.timer.onBootSec;
        OnUnitActiveSec = mirror.timer.onUnitActiveSec;
        Persistent = true;
      };
    };
  };
}
