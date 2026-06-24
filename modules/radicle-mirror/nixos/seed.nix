{
  config,
  lib,
  ...
}:
let
  cfg = config.services.radicleMirror;
  seed = cfg.seed;
in
{
  options.services.radicleMirror = {
    enable = lib.mkEnableOption "a Radicle seed and mirror deployment";

    domain = lib.mkOption {
      type = lib.types.str;
      description = "Public DNS name for the Radicle seed.";
    };

    seed = {
      enable = lib.mkEnableOption "the public Radicle seed node";

      privateKeyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Path to the Radicle node private key.";
      };

      publicKey = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Radicle node public key.";
      };

      nodeId = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Radicle seed node ID.";
      };

      listenAddress = lib.mkOption {
        type = lib.types.str;
        default = "[::]";
        description = "Address the Radicle node listens on.";
      };

      listenPort = lib.mkOption {
        type = lib.types.port;
        default = 8776;
        description = "TCP port the Radicle node listens on.";
      };

      openFirewall = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Open the firewall for the public Radicle node.";
      };

      httpListenAddress = lib.mkOption {
        type = lib.types.str;
        default = "127.0.0.1";
        description = "Address the Radicle HTTP API listens on.";
      };

      httpListenPort = lib.mkOption {
        type = lib.types.port;
        default = 8080;
        description = "Port the Radicle HTTP API listens on.";
      };

      externalAddresses = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        defaultText = lib.literalExpression ''[ "$${config.services.radicleMirror.domain}:8776" ]'';
        description = "Public addresses announced by the Radicle node.";
      };

      extraNodeSettings = lib.mkOption {
        type = lib.types.attrs;
        default = { };
        description = "Additional settings merged into services.radicle.settings.node.";
      };
    };
  };

  config = lib.mkIf (cfg.enable && seed.enable) {
    assertions = [
      {
        assertion = seed.privateKeyFile != null;
        message = "services.radicleMirror.seed.privateKeyFile must be set.";
      }
      {
        assertion = seed.publicKey != null;
        message = "services.radicleMirror.seed.publicKey must be set.";
      }
    ];

    services.radicle = {
      enable = true;
      privateKey = seed.privateKeyFile;
      publicKey = seed.publicKey;

      node = {
        listenAddress = seed.listenAddress;
        listenPort = seed.listenPort;
        openFirewall = seed.openFirewall;
      };

      httpd = {
        enable = true;
        listenAddress = seed.httpListenAddress;
        listenPort = seed.httpListenPort;
      };

      settings.node = lib.recursiveUpdate {
        alias = cfg.domain;
        externalAddresses =
          if seed.externalAddresses == [ ] then
            [ "${cfg.domain}:${toString seed.listenPort}" ]
          else
            seed.externalAddresses;
        limits = {
          routingMaxSize = 10000;
          fetchConcurrency = 8;
          maxOpenFiles = 16384;
          fetchPackReceive = "5 GiB";
          rate = {
            inbound = {
              fillRate = 50.0;
              capacity = 8192;
            };
            outbound = {
              fillRate = 50.0;
              capacity = 8192;
            };
          };
          connection = {
            inbound = 512;
            outbound = 128;
          };
        };
        seedingPolicy.default = "block";
        workers = 32;
      } seed.extraNodeSettings;
    };
  };
}
