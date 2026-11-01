# Run with nix eval --impure --json --file modules/ralph/tests/eval.nix.
let
  flake = builtins.getFlake (toString ../../..);
  lib = flake.inputs.nixpkgs.lib;
  common = {
    enable = true;
    origin = "https://git.example.org/owner/repo.git";
    repository = "owner/repo";
    forgejoApi = "https://git.example.org/api/v1/repos/owner/repo";
    botLogin = "ralph";
    openaiKeyFile = "/run/secrets/key";
    webhookSecretFile = "/run/secrets/hook";
    forgejoTokenFile = "/run/secrets/token";
  };
  pair = {
    core = common // {
      serviceName = "ralph";
      reportDir = "/var/lib/reports/core";
      reportBaseUrl = "https://review.example.org/core";
    };
    gui = common // {
      port = 8766;
      repository = "owner/gui";
    };
  };
  evaluate =
    instances:
    (lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        flake.nixosModules.ralph
        {
          boot.loader.grub.devices = [ "/dev/sda" ];
          fileSystems."/" = {
            device = "/dev/sda1";
            fsType = "ext4";
          };
          system.stateVersion = "26.05";
          services.ralph.instances = instances;
        }
      ];
    }).config;
  cfg = evaluate pair;
  failures = c: map (x: x.message) (builtins.filter (x: !x.assertion) c.assertions);
  rejects = override: failures (evaluate (pair // { gui = pair.gui // override; })) != [ ];
  results = {
    isolated =
      failures cfg == [ ]
      && cfg.systemd.services.ralph.serviceConfig.StateDirectory == "ralph"
      && cfg.systemd.services.ralph-gui.serviceConfig.StateDirectory == "ralph-gui"
      &&
        cfg.systemd.services.ralph.environment.RALPH_WRITE_VERIFIER_CGROUP
        == "/sys/fs/cgroup/system.slice/ralph.service"
      &&
        cfg.systemd.services.ralph-gui.environment.RALPH_WRITE_VERIFIER_CGROUP
        == "/sys/fs/cgroup/system.slice/ralph-gui.service"
      && cfg.systemd.services.ralph-stats.requires == [ "ralph.service" ]
      && !(cfg.systemd.services ? ralph-gui-stats)
      && !(cfg.systemd.services ? ralph-gui-telegram);
    duplicatePort = rejects { port = 8765; };
    sharedState = rejects { stateDir = "/var/lib/ralph"; };
    nestedReport = rejects { stateDir = "/var/lib/reports/core/gui"; };
    companionCollision = rejects { serviceName = "ralph-stats"; };
    duplicateRepository = rejects { repository = "owner/repo"; };
    disabled =
      !(evaluate {
        gui = {
          enable = false;
        };
      }).systemd.services ? ralph-gui;
    explicitCurrentOptions = rejects {
      engine = "external";
      routingMode = "enabled";
    };
  };
in
assert lib.all (result: result) (builtins.attrValues results);
results
