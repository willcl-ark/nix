# Run with nix eval --impure --json --file modules/ralph/tests/eval.nix.
let
  flake = builtins.getFlake (toString ../../..);
  lib = flake.inputs.nixpkgs.lib;
  repository = name: {
    origin = "https://git.example.org/owner/${name}.git";
    repository = "owner/${name}";
    forgejoApi = "https://git.example.org/api/v1/repos/owner/${name}";
    botLogin = "ralph";
    webhookSecretFile = "/run/secrets/${name}-hook";
    forgejoTokenFile = "/run/secrets/token";
  };
  evaluate =
    overrides:
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
          services.ralph = {
            enable = true;
            openaiKeyFile = "/run/secrets/key";
            reportDir = "/var/www/ralph";
            reportBaseUrl = "https://reviews.example.org/report";
            archiveBaseUrl = "https://reviews.example.org/traces";
            repositories = {
              core = repository "core";
              gui = repository "gui" // {
                strategy = "gui";
              };
            };
          };
        }
        overrides
      ];
    }).config;
  failures = c: map (x: x.message) (builtins.filter (x: !x.assertion) c.assertions);
  cfg = evaluate { };
  results = {
    sharedCoordinator =
      failures cfg == [ ]
      && cfg.systemd.services ? ralph
      && !(cfg.systemd.services ? ralph-gui)
      && lib.hasInfix "--repositories" cfg.systemd.services.ralph.serviceConfig.ExecStart
      && lib.hasInfix "--repositories" cfg.systemd.services.ralph-stats.serviceConfig.ExecStart
      && lib.hasInfix "--repositories" cfg.systemd.services.ralph-followup.serviceConfig.ExecStart;
    archiveUrlRequired =
      failures (evaluate {
        services.ralph.archiveBaseUrl = lib.mkForce null;
      }) != [ ];
    publicationRequiresCredentials =
      failures (evaluate {
        services.ralph.repositories.gui.githubPublication = "full";
      }) != [ ];
    duplicateSource =
      failures (evaluate {
        services.ralph.repositories.gui.forgejoApi =
          lib.mkForce "https://git.example.org/api/v1/repos/owner/core";
      }) != [ ];
    separatePublicState =
      failures (evaluate {
        services.ralph.reportDir = lib.mkForce "/var/lib/ralph/reports";
      }) != [ ];
  };
in
assert lib.all (result: result) (builtins.attrValues results);
results
