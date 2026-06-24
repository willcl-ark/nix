{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.radicleMirror;
  frontend = cfg.frontend;
  frontendDomain = if frontend.domain == null then cfg.domain else frontend.domain;

  radicleExplorerConfig = builtins.toJSON {
    nodes = {
      fallbackPublicExplorer = frontend.fallbackPublicExplorer;
      requiredApiVersion = "~0.18.0";
      defaultHttpdPort = 443;
      defaultLocalHttpdPort = cfg.seed.httpListenPort;
      defaultHttpdScheme = "https";
    };
    source.commitsPerPage = 30;
    supportWebsite = "https://radicle.zulipchat.com";
    deploymentId = null;
    preferredSeeds = [
      {
        hostname = frontendDomain;
        port = 443;
        scheme = "https";
      }
    ];
  };

  radicleExplorer = pkgs.runCommand "radicle-explorer-${frontendDomain}" { } ''
    cp -R ${pkgs.radicle-explorer} "$out"
    chmod -R u+w "$out"
    substituteInPlace "$out/index.html" \
      --replace-fail '<head>' '<head>
    <script type="text/javascript">
      window.__CONFIG__ = ${radicleExplorerConfig};
    </script>'
  '';
in
{
  options.services.radicleMirror.frontend = {
    enable = lib.mkEnableOption "Radicle Explorer frontend";

    domain = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      defaultText = lib.literalExpression "config.services.radicleMirror.domain";
      description = "Virtual host for the Radicle Explorer frontend.";
    };

    fallbackPublicExplorer = lib.mkOption {
      type = lib.types.str;
      default = "https://app.radicle.xyz/nodes/$host/$rid$path";
      description = "Fallback public explorer URL template.";
    };
  };

  config = lib.mkIf (cfg.enable && frontend.enable) {
    services.caddy.virtualHosts.${frontendDomain}.extraConfig = ''
      handle /api/* {
        reverse_proxy ${cfg.seed.httpListenAddress}:${toString cfg.seed.httpListenPort}
      }

      handle {
        root * ${radicleExplorer}
        try_files {path} /index.html
        file_server
      }
    '';
  };
}
