{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.stuntman;
  stunserver = lib.getExe' pkgs.stuntman "stunserver";
  btcpunchRendezvous = ../scripts/btcpunch_rendezvous.py;
in
{
  options.services.stuntman = {
    enable = lib.mkEnableOption "STUNTMAN STUN server";

    stunPort = lib.mkOption {
      type = lib.types.port;
      default = 3478;
      description = "TCP and UDP port for the STUNTMAN server.";
    };

    rendezvousAddress = lib.mkOption {
      type = lib.types.str;
      default = "0.0.0.0";
      description = "Address the btcpunch rendezvous server binds to.";
    };

    rendezvousPort = lib.mkOption {
      type = lib.types.port;
      default = 3479;
      description = "UDP port for the btcpunch rendezvous server.";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Open firewall ports for STUNTMAN and the btcpunch rendezvous server.";
    };
  };

  config = lib.mkIf cfg.enable {
    networking.firewall = lib.mkIf cfg.openFirewall {
      allowedTCPPorts = [ cfg.stunPort ];
      allowedUDPPorts = [
        cfg.stunPort
        cfg.rendezvousPort
      ];
    };

    systemd.services.stuntman-udp = {
      description = "STUNTMAN STUN server (UDP)";
      after = [ "network.target" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        DynamicUser = true;
        ExecStart = "${stunserver} --protocol udp --primaryport ${toString cfg.stunPort}";
        Restart = "on-failure";
        RestartSec = "5s";
      };
    };

    systemd.services.stuntman-tcp = {
      description = "STUNTMAN STUN server (TCP)";
      after = [ "network.target" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        DynamicUser = true;
        ExecStart = "${stunserver} --protocol tcp --primaryport ${toString cfg.stunPort}";
        Restart = "on-failure";
        RestartSec = "5s";
      };
    };

    systemd.services.btcpunch-rendezvous = {
      description = "btcpunch UDP rendezvous server";
      after = [ "network.target" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        DynamicUser = true;
        ExecStart = "${pkgs.python3.interpreter} ${btcpunchRendezvous} --bind ${cfg.rendezvousAddress}:${toString cfg.rendezvousPort}";
        Restart = "on-failure";
        RestartSec = "5s";
      };
    };
  };
}
