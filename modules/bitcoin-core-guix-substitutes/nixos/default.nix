{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.bitcoinCoreGuixSubstitutes;

  runtimeDirectory = "guix-publish";
  runtimePath = "/run/${runtimeDirectory}";
  publishCacheDirectory = "${cfg.dataDir}/publish-cache";
  publicDirectory = "${cfg.dataDir}/public";
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

  publicKeyRuntimePath = "${runtimePath}/signing-key.pub";
  privateKeyRuntimePath = "${runtimePath}/signing-key.sec";
  publicKeyWebPath = "${publicDirectory}/signing-key.pub";
  publicKeySignatureWebPath = "${publicDirectory}/signing-key.pub.asc";
  landingPageDirectory = pkgs.writeTextDir "index.html" ''
        <html><head><title>GNU Guix Substitute Server</title></head>
        <body>
        <h1>GNU Guix Bitcoin Core Substitute Server</h1>
        <p>Hi, <a href="https://guix.gnu.org/manual/en/html_node/Invoking-guix-publish.html"><tt>guix publish</tt></a> speaking. Welcome!</p>
        <p>Here is the <a href="signing-key.pub"><tt>signing key</tt></a> for this server.</p>
        <h2>Usage</h2>
        <h4>1. Download the signing key and signature</h3>
        <pre><code>curl -fLO https://${cfg.domain}/signing-key.pub
    curl -fLO https://${cfg.domain}/signing-key.pub.asc</code></pre>
        <h4>2. Verify the signing key</h3>
        <p>The OpenPGP key for this signature is published in <a href="https://github.com/bitcoin-core/guix.sigs"><tt>bitcoin-core/guix.sigs</tt></a> as <a href="https://raw.githubusercontent.com/bitcoin-core/guix.sigs/refs/heads/main/builder-keys/willcl-ark.gpg"><tt>builder-keys/willcl-ark.gpg</tt></a>.</p>
        <pre><code>curl -fL https://raw.githubusercontent.com/bitcoin-core/guix.sigs/refs/heads/main/builder-keys/willcl-ark.gpg -o willcl-ark.gpg
    gpg --import willcl-ark.gpg
    gpg --verify signing-key.pub.asc signing-key.pub</code></pre>
        <h4>3. Authorize the key with guix</h3>
        <p>Either authorize the Guix signing key as root:</p>
        <pre><code>guix archive --authorize &lt; signing-key.pub</code></pre>
        <p>Or, with sudo:</p>
        <pre><code>sudo guix archive --authorize &lt; signing-key.pub</code></pre>
        <h4>4. Use this substitute server</h3>
        <p>For bitcoin build scripts under <tt>./contrib/guix</tt>, set <tt>SUBSTITUTE_URLS</tt>:</p>
        <pre><code>export SUBSTITUTE_URLS='https://${cfg.domain} https://ci.guix.gnu.org'</code></pre>
        <p>To make this more permanent you can change the default list of substitute servers by starting <tt>guix-daemon</tt> with <tt>--substitute-urls</tt>. You will likely need to edit your init script:</p>
        <pre><code>guix-daemon &lt;cmd&gt; --substitute-urls='https://${cfg.domain} https://ci.guix.gnu.org'</code></pre>
        <p>Or override the default list for one <tt>guix</tt> invocation:</p>
        <pre><code>guix &lt;cmd&gt; --substitute-urls='https://${cfg.domain} https://ci.guix.gnu.org'</code></pre>
        </body></html>
  '';

  sdkSetup = lib.concatStringsSep "\n" (
    map (sdk: ''
      if [ ! -d ${cfg.dataDir}/macos-sdks/${sdk}-extracted-SDK-with-libcxx-headers ]; then
        curl -fL --retry 3 \
          ${cfg.macosSdkBaseUrl}/${sdk}-extracted-SDK-with-libcxx-headers.tar \
          -o ${cfg.dataDir}/macos-sdks/${sdk}-extracted-SDK-with-libcxx-headers.tar
        tar -C ${cfg.dataDir}/macos-sdks \
          -xaf ${cfg.dataDir}/macos-sdks/${sdk}-extracted-SDK-with-libcxx-headers.tar
      fi
    '') cfg.macosSdks
  );
  prewarmSubstitutes = ''
    prewarm_url=http://${cfg.publishAddress}:${toString cfg.publishPort}

    prewarm_store_path() {
      path="$1"
      hash="$(basename "$path" | cut -d- -f1)"
      until curl -fsS -o /dev/null "$prewarm_url/$hash.narinfo"; do
        sleep 10
      done
    }

    if version="$(git describe --exact-match HEAD 2> /dev/null)"; then
      version="''${version#v}"
    else
      version="$(git rev-parse --short=12 HEAD)"
    fi

    profiles_dir="guix-build-$version/var/profiles"
    for host in ${lib.escapeShellArgs bitcoinGuixHosts}; do
      profile="$profiles_dir/$host"
      if [ ! -e "$profile" ]; then
        echo "ERR: Guix profile $profile does not exist"
        exit 1
      fi
    done

    echo "Prewarming Guix substitute closure..."
    for host in ${lib.escapeShellArgs bitcoinGuixHosts}; do
      guix gc --requisites "$(readlink -f "$profiles_dir/$host")"
    done | sort -u | while IFS= read -r path; do
      prewarm_store_path "$path"
    done
  ''
  + lib.concatStringsSep "\n" (
    map (host: ''
      echo "Checking Guix substitute availability for ${host}..."
      until JOBS=${toString cfg.buildJobs} \
        ADDITIONAL_GUIX_TIMEMACHINE_FLAGS="${cfg.additionalGuixTimemachineFlags}" \
        env HOST=${host} \
          guix time-machine \
            --url=https://codeberg.org/guix/guix.git \
            --commit=c5eee3336cc1d10a3cc1c97fde2809c3451624d3 \
            ${cfg.additionalGuixTimemachineFlags} \
            -- weather \
              --substitute-urls=$prewarm_url \
              -m contrib/guix/manifest_build.scm \
          | grep -q '100.0% substitutes available'; do
        sleep 10
      done
    '') bitcoinGuixHosts
  );
in
{
  options.services.bitcoinCoreGuixSubstitutes = {
    enable = lib.mkEnableOption "Bitcoin Core Guix substitute server";

    domain = lib.mkOption {
      type = lib.types.str;
      description = "Public HTTPS domain for the substitute server.";
    };

    publishAddress = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Address guix publish listens on.";
    };

    publishPort = lib.mkOption {
      type = lib.types.port;
      default = 8181;
      description = "Port guix publish listens on.";
    };

    storeDir = lib.mkOption {
      type = lib.types.str;
      default = "/gnu/store";
      description = "Guix store directory that must exist before guix-daemon starts.";
    };

    dataDir = lib.mkOption {
      type = lib.types.str;
      default = "/gnu/guix-bitcoin";
      description = "Data directory for the Bitcoin Core checkout, Guix build caches, and public key files.";
    };

    buildUser = lib.mkOption {
      type = lib.types.str;
      default = "guix-bitcoin-build";
      description = "System user that runs Bitcoin Core Guix builds.";
    };

    buildGroup = lib.mkOption {
      type = lib.types.str;
      default = "guix-bitcoin-build";
      description = "System group that runs Bitcoin Core Guix builds.";
    };

    signingKey = {
      publicFile = lib.mkOption {
        type = lib.types.path;
        description = "Sops file containing the Guix substitute signing public key.";
      };

      privateFile = lib.mkOption {
        type = lib.types.path;
        description = "Sops file containing the Guix substitute signing private key.";
      };

      signatureFile = lib.mkOption {
        type = lib.types.path;
        description = "Detached OpenPGP signature for the Guix substitute signing public key.";
      };
    };

    bitcoinRepository = lib.mkOption {
      type = lib.types.str;
      default = "https://github.com/bitcoin/bitcoin";
      description = "Bitcoin Core Git repository to build.";
    };

    bitcoinRemote = lib.mkOption {
      type = lib.types.str;
      default = "origin";
      description = "Remote name used for the Bitcoin Core checkout.";
    };

    bitcoinBranch = lib.mkOption {
      type = lib.types.str;
      default = "master";
      description = "Branch to build from the Bitcoin Core repository.";
    };

    buildJobs = lib.mkOption {
      type = lib.types.ints.positive;
      default = 16;
      description = "JOBS value passed to contrib/guix/guix-build.";
    };

    macosSdks = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "macOS SDK archives to download before running Guix builds.";
    };

    macosSdkBaseUrl = lib.mkOption {
      type = lib.types.str;
      default = "https://bitcoincore.org/depends-sources/sdks";
      description = "Base URL for Bitcoin Core macOS SDK archives.";
    };

    additionalGuixTimemachineFlags = lib.mkOption {
      type = lib.types.str;
      default = "--url=https://github.com/Millak/guix.git";
      description = "Flags passed through ADDITIONAL_GUIX_TIMEMACHINE_FLAGS.";
    };

    buildTimer = {
      onBootSec = lib.mkOption {
        type = lib.types.str;
        default = "30m";
        description = "Delay before the first scheduled Bitcoin Core Guix build.";
      };

      onUnitActiveSec = lib.mkOption {
        type = lib.types.str;
        default = "2d";
        description = "Interval between scheduled Bitcoin Core Guix builds.";
      };

      randomizedDelaySec = lib.mkOption {
        type = lib.types.str;
        default = "1h";
        description = "Randomized delay for scheduled Bitcoin Core Guix builds.";
      };
    };

    cleanup = {
      maxAgeDays = lib.mkOption {
        type = lib.types.ints.positive;
        default = 14;
        description = "Age in days after which guix-build-* work directories are removed.";
      };

      randomizedDelaySec = lib.mkOption {
        type = lib.types.str;
        default = "1h";
        description = "Randomized delay for the cleanup timer.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    sops.secrets."guix-signing-key.pub" = {
      sopsFile = cfg.signingKey.publicFile;
      format = "binary";
      owner = "guix-publish";
      group = "guix-publish";
      mode = "0444";
    };
    sops.secrets."guix-signing-key.sec" = {
      sopsFile = cfg.signingKey.privateFile;
      format = "binary";
      owner = "guix-publish";
      group = "guix-publish";
      mode = "0400";
    };

    services.guix = {
      enable = true;

      publish = {
        enable = true;
        generateKeyPair = false;
        port = cfg.publishPort;
        extraArgs = [
          "--listen=${cfg.publishAddress}"
          "--cache=${publishCacheDirectory}"
          "--compression=zstd:6"
          "--cache-bypass-threshold=0"
          "--ttl=30d"
          "--workers=${toString cfg.buildJobs}"
          "--public-key=${publicKeyRuntimePath}"
          "--private-key=${privateKeyRuntimePath}"
        ];
      };
    };

    services.caddy.virtualHosts.${cfg.domain}.extraConfig = ''
      handle /signing-key.pub {
        root * ${publicDirectory}
        file_server
      }

      handle /signing-key.pub.asc {
        root * ${publicDirectory}
        file_server
      }

      handle / {
        root * ${landingPageDirectory}
        file_server
      }

      handle {
        reverse_proxy ${cfg.publishAddress}:${toString cfg.publishPort}
      }
    '';

    users.users.${cfg.buildUser} = {
      isSystemUser = true;
      group = cfg.buildGroup;
      home = cfg.dataDir;
      createHome = true;
    };
    users.groups.${cfg.buildGroup} = { };

    systemd.tmpfiles.rules = [
      "d ${cfg.storeDir} 0755 root root -"
      "d ${publishCacheDirectory} 0755 guix-publish guix-publish -"
      "d ${publicDirectory} 0755 root root -"
      "d ${cfg.dataDir} 0750 ${cfg.buildUser} ${cfg.buildGroup} -"
      "d ${cfg.dataDir}/bitcoin 0750 ${cfg.buildUser} ${cfg.buildGroup} -"
      "d ${cfg.dataDir}/cache 0750 ${cfg.buildUser} ${cfg.buildGroup} -"
      "d ${cfg.dataDir}/macos-sdks 0750 ${cfg.buildUser} ${cfg.buildGroup} -"
      "d ${cfg.dataDir}/sources 0750 ${cfg.buildUser} ${cfg.buildGroup} -"
    ];

    systemd.services.guix-publish = {
      after = [ "sops-install-secrets.service" ];
      wants = [ "sops-install-secrets.service" ];
      serviceConfig.ExecStartPre = [
        "+${pkgs.coreutils}/bin/install -d -m 0755 -o guix-publish -g guix-publish ${publishCacheDirectory}"
        "+${pkgs.coreutils}/bin/install -d -m 0755 -o root -g root ${publicDirectory}"
        "+${pkgs.coreutils}/bin/install -d -m 0750 -o guix-publish -g guix-publish ${runtimePath}"
        "+${pkgs.coreutils}/bin/install -m 0444 -o guix-publish -g guix-publish ${
          config.sops.secrets."guix-signing-key.pub".path
        } ${publicKeyRuntimePath}"
        "+${pkgs.coreutils}/bin/install -m 0444 -o root -g root ${
          config.sops.secrets."guix-signing-key.pub".path
        } ${publicKeyWebPath}"
        "+${pkgs.coreutils}/bin/install -m 0444 -o root -g root ${cfg.signingKey.signatureFile} ${publicKeySignatureWebPath}"
        "+${pkgs.coreutils}/bin/install -m 0440 -o root -g guix-publish ${
          config.sops.secrets."guix-signing-key.sec".path
        } ${privateKeyRuntimePath}"
      ];
      serviceConfig.RuntimeDirectory = runtimeDirectory;
      serviceConfig.RuntimeDirectoryMode = "0750";
    };

    systemd.services.guix-bitcoin-build = {
      description = "Build Bitcoin Core with Guix";
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
      path = [
        pkgs.bash
        pkgs.coreutils
        pkgs.curl
        pkgs.findutils
        pkgs.getent
        pkgs.gnumake
        pkgs.gnugrep
        pkgs.gnused
        pkgs.gnutar
        config.services.guix.package
        pkgs.git
      ];
      script = ''
        set -euo pipefail

        if [ ! -d ${cfg.dataDir}/bitcoin/.git ]; then
          git clone ${cfg.bitcoinRepository} ${cfg.dataDir}/bitcoin
        fi

        cd ${cfg.dataDir}/bitcoin
        git fetch ${cfg.bitcoinRemote} ${cfg.bitcoinBranch}
        commit="$(git rev-parse ${cfg.bitcoinRemote}/${cfg.bitcoinBranch})"
        if [ -f ${cfg.dataDir}/last-built-commit ] \
          && [ "$(cat ${cfg.dataDir}/last-built-commit)" = "$commit" ]; then
          echo "Bitcoin Core ${cfg.bitcoinBranch} is already built at $commit; skipping."
          exit 0
        fi

        git reset --hard ${cfg.bitcoinRemote}/${cfg.bitcoinBranch}
        git submodule update --init --recursive

        ${sdkSetup}

        JOBS=${toString cfg.buildJobs} \
        SOURCES_PATH=${cfg.dataDir}/sources \
        BASE_CACHE=${cfg.dataDir}/cache \
        SDK_PATH=${cfg.dataDir}/macos-sdks \
        ADDITIONAL_GUIX_TIMEMACHINE_FLAGS="${cfg.additionalGuixTimemachineFlags}" \
          ./contrib/guix/guix-build

        ${prewarmSubstitutes}

        printf '%s\n' "$commit" > ${cfg.dataDir}/last-built-commit
      '';
      serviceConfig = {
        Type = "oneshot";
        User = cfg.buildUser;
        Group = cfg.buildGroup;
        ExecStartPre = [
          "+${pkgs.coreutils}/bin/install -d -m 0750 -o ${cfg.buildUser} -g ${cfg.buildGroup} ${cfg.dataDir}"
          "+${pkgs.coreutils}/bin/install -d -m 0750 -o ${cfg.buildUser} -g ${cfg.buildGroup} ${cfg.dataDir}/bitcoin"
          "+${pkgs.coreutils}/bin/install -d -m 0750 -o ${cfg.buildUser} -g ${cfg.buildGroup} ${cfg.dataDir}/cache"
          "+${pkgs.coreutils}/bin/install -d -m 0750 -o ${cfg.buildUser} -g ${cfg.buildGroup} ${cfg.dataDir}/macos-sdks"
          "+${pkgs.coreutils}/bin/install -d -m 0750 -o ${cfg.buildUser} -g ${cfg.buildGroup} ${cfg.dataDir}/sources"
        ];
      };
    };

    systemd.timers.guix-bitcoin-build = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = cfg.buildTimer.onBootSec;
        OnUnitActiveSec = cfg.buildTimer.onUnitActiveSec;
        Persistent = true;
        RandomizedDelaySec = cfg.buildTimer.randomizedDelaySec;
      };
    };

    systemd.services.guix-bitcoin-build-cleanup = {
      description = "Clean old Bitcoin Core Guix build work directories";
      serviceConfig = {
        Type = "oneshot";
        User = cfg.buildUser;
        Group = cfg.buildGroup;
      };
      path = [
        pkgs.findutils
      ];
      script = ''
        find ${cfg.dataDir}/bitcoin \
          -maxdepth 1 \
          -type d \
          -name 'guix-build-*' \
          -mtime +${toString cfg.cleanup.maxAgeDays} \
          -exec rm -rf {} +
      '';
    };

    systemd.timers.guix-bitcoin-build-cleanup = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = "daily";
        Persistent = true;
        RandomizedDelaySec = cfg.cleanup.randomizedDelaySec;
      };
    };
  };
}
