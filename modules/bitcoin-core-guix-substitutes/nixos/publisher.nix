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
  publicKeyRuntimePath = "${runtimePath}/signing-key.pub";
  privateKeyRuntimePath = "${runtimePath}/signing-key.sec";
  publicKeyWebPath = "${publicDirectory}/signing-key.pub";
  publicKeySignatureWebPath = "${publicDirectory}/signing-key.pub.asc";
  dataDirOwner = if cfg.builder.enable then cfg.builder.buildUser else "root";
  dataDirGroup = if cfg.builder.enable then cfg.builder.buildGroup else "root";

  landingPageDirectory = pkgs.writeTextDir "index.html" ''
    <html><head><title>GNU Guix Substitute Server</title></head>
    <body>
    <h1>GNU Guix Bitcoin Core Substitute Server</h1>
    <p>Hi, <a href="https://guix.gnu.org/manual/en/html_node/Invoking-guix-publish.html"><tt>guix publish</tt></a> speaking. Welcome!</p>
    <p>Here is the <a href="signing-key.pub"><tt>signing key</tt></a> for this server.</p>
    <h2>Usage</h2>
    <h4>1. Download the signing key and signature</h4>
    <pre><code>curl -fLO https://${cfg.domain}/signing-key.pub
    curl -fLO https://${cfg.domain}/signing-key.pub.asc</code></pre>
    <h4>2. Verify the signing key</h4>
    <p>The OpenPGP key for this signature is published in <a href="https://github.com/bitcoin-core/guix.sigs"><tt>bitcoin-core/guix.sigs</tt></a> as <a href="https://raw.githubusercontent.com/bitcoin-core/guix.sigs/refs/heads/main/builder-keys/willcl-ark.gpg"><tt>builder-keys/willcl-ark.gpg</tt></a>.</p>
    <pre><code>curl -fL https://raw.githubusercontent.com/bitcoin-core/guix.sigs/refs/heads/main/builder-keys/willcl-ark.gpg -o willcl-ark.gpg
    gpg --import willcl-ark.gpg
    gpg --verify signing-key.pub.asc signing-key.pub</code></pre>
    <h4>3. Authorize the key with guix</h4>
    <p>Either authorize the Guix signing key as root:</p>
    <pre><code>guix archive --authorize &lt; signing-key.pub</code></pre>
    <p>Or, with sudo:</p>
    <pre><code>sudo guix archive --authorize &lt; signing-key.pub</code></pre>
    <h4>4. Use this substitute server</h4>
    <p>For bitcoin build scripts under <tt>./contrib/guix</tt>, set <tt>SUBSTITUTE_URLS</tt>:</p>
    <pre><code>export SUBSTITUTE_URLS='https://${cfg.domain} https://ci.guix.gnu.org'</code></pre>
    <p>To make this more permanent you can change the default list of substitute servers by starting <tt>guix-daemon</tt> with <tt>--substitute-urls</tt>. You will likely need to edit your init script:</p>
    <pre><code>guix-daemon &lt;cmd&gt; --substitute-urls='https://${cfg.domain} https://ci.guix.gnu.org'</code></pre>
    <p>Or override the default list for one <tt>guix</tt> invocation:</p>
    <pre><code>guix &lt;cmd&gt; --substitute-urls='https://${cfg.domain} https://ci.guix.gnu.org'</code></pre>
    </body></html>
  '';
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

    publishWorkers = lib.mkOption {
      type = lib.types.ints.positive;
      default = 16;
      description = "Number of guix publish worker threads.";
    };

    storeDir = lib.mkOption {
      type = lib.types.str;
      default = "/gnu/store";
      description = "Guix store directory that must exist before guix-daemon starts.";
    };

    dataDir = lib.mkOption {
      type = lib.types.str;
      default = "/gnu/guix-bitcoin";
      description = "Data directory for Guix publish state and optional builder jobs.";
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
          "--workers=${toString cfg.publishWorkers}"
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

    systemd.tmpfiles.rules = [
      "d ${cfg.storeDir} 0755 root root -"
      "d ${cfg.dataDir} 0751 ${dataDirOwner} ${dataDirGroup} -"
      "z ${cfg.dataDir} 0751 ${dataDirOwner} ${dataDirGroup} -"
      "d ${publishCacheDirectory} 0755 guix-publish guix-publish -"
      "d ${publicDirectory} 0755 root root -"
    ];

    systemd.services.guix-publish = {
      after = [ "sops-install-secrets.service" ];
      wants = [ "sops-install-secrets.service" ];
      serviceConfig.ExecStartPre = [
        "+${pkgs.coreutils}/bin/chmod 0751 ${cfg.dataDir}"
        "+${pkgs.coreutils}/bin/install -d -m 0751 -o ${dataDirOwner} -g ${dataDirGroup} ${cfg.dataDir}"
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
  };
}
