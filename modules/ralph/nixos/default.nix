{
  config,
  lib,
  pkgs,
  ...
}:
let
  instances = lib.filterAttrs (_: cfg: cfg.enable) config.services.ralph.instances;
  values = builtins.attrValues instances;
  unique = xs: builtins.length xs == builtins.length (lib.unique xs);
  directories = map (path: toString (builtins.toPath path)) (
    lib.concatMap (cfg: [ cfg.stateDir ] ++ lib.optional (cfg.reportDir != null) cfg.reportDir) values
  );
  unitNames = lib.concatMap (
    cfg:
    [ cfg.serviceName ]
    ++ lib.optionals (cfg.reportDir != null) [
      "${cfg.serviceName}-stats"
      "${cfg.serviceName}-followup"
    ]
    ++ lib.optional cfg.telegram.enable "${cfg.serviceName}-telegram"
  ) values;
  overlaps = a: b: a == b || lib.hasPrefix "${a}/" b || lib.hasPrefix "${b}/" a;
in
{
  options.services.ralph.instances = lib.mkOption {
    default = { };
    description = "Independent Ralph reviewers, one per repository.";
    type = lib.types.attrsOf (
      lib.types.submoduleWith {
        shorthandOnlyDefinesConfig = true;
        specialArgs = { inherit pkgs; };
        modules = [ ./instance.nix ];
      }
    );
  };

  config = {
    systemd.services = lib.mkMerge (map (cfg: cfg.systemConfig.systemd.services or { }) values);
    systemd.timers = lib.mkMerge (map (cfg: cfg.systemConfig.systemd.timers or { }) values);
    systemd.tmpfiles.rules = lib.concatMap (cfg: cfg.systemConfig.systemd.tmpfiles.rules or [ ]) values;
    users.users = lib.mkMerge (map (cfg: cfg.systemConfig.users.users or { }) values);
    users.groups = lib.mkMerge (map (cfg: cfg.systemConfig.users.groups or { }) values);
    assertions = lib.concatMap (cfg: cfg.systemConfig.assertions or [ ]) values ++ [
      {
        assertion = unique unitNames;
        message = "Ralph instances must use distinct reviewer and companion service names.";
      }
      {
        assertion = unique (map (cfg: cfg.user) values);
        message = "Ralph instances must use distinct system users.";
      }
      {
        assertion = unique (map (cfg: cfg.group) values);
        message = "Ralph instances must use distinct system groups.";
      }
      {
        assertion = unique (map (cfg: cfg.repository) values);
        message = "Each repository must have only one Ralph instance.";
      }
      {
        assertion = unique (map (cfg: toString cfg.port) values);
        message = "Ralph instances must use distinct listening ports.";
      }
      {
        assertion = lib.all (
          a: builtins.length (builtins.filter (b: overlaps a b) directories) == 1
        ) directories;
        message = "Ralph instance state and report directories must not overlap.";
      }
      {
        assertion = unique (
          map (cfg: cfg.telegram.botTokenFile) (builtins.filter (cfg: cfg.telegram.enable) values)
        );
        message = "Ralph Telegram monitors must use distinct bot tokens; one bot cannot poll for multiple instances.";
      }
    ];
  };
}
