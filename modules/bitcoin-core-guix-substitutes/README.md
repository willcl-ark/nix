# Bitcoin Core Guix Substitutes NixOS Module

Reusable NixOS module for publishing Bitcoin Core Guix build substitutes.

The base module owns the Guix daemon, `guix publish`, public signing-key files,
the landing page, and Caddy virtual host wiring. The optional builder owns the
source-job queue, nightly source submission, Bitcoin worker, and retention
timers.

Host-local policy such as cloud instance lifecycle, Guix offload machine
definitions, credentials, and archive-key trust stays outside this module. If
`builder.nativeSystems` includes systems that are not locally buildable, configure
Guix offloading in the host configuration.

## Publisher Usage

```nix
{
  inputs.will-nix.url = "github:willcl-ark/nix";

  outputs =
    { nixpkgs, will-nix, ... }:
    {
      nixosConfigurations.host = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [
          will-nix.nixosModules.bitcoin-core-guix-substitutes
          {
            services.bitcoinCoreGuixSubstitutes = {
              enable = true;
              domain = "guix.example.org";
              dataDir = "/gnu/guix-bitcoin";

              signingKey = {
                publicFile = ./secrets/signing-key.pub;
                privateFile = ./secrets/signing-key.sec;
                signatureFile = ./secrets/signing-key.pub.asc;
              };
            };
          }
        ];
      };
    };
}
```

## Builder Usage

```nix
{
  services.bitcoinCoreGuixSubstitutes = {
    enable = true;

    builder = {
      enable = true;
      nativeSystems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      schedule.onCalendar = "*-*-* 06:00:00 UTC";
      retention.failedJobMaxAgeDays = 30;
    };
  };
}
```

The builder installs `guix-bitcoin-submit` as an operator tool:

```bash
guix-bitcoin-submit [--repository REPOSITORY] COMMIT
```

`COMMIT` must be a lowercase full 40-character commit hash. If `--repository`
is omitted, the configured Bitcoin Core repository is used. The server fetches
only that commit into its local Git object store, pins the accepted job, and
builds the `contrib/guix/manifest_*.scm` files from a detached worktree. This is
a trusted-operator interface; the repository is the source used to obtain the
requested commit, not a provenance boundary.

## Interface

Publisher options:

- `services.bitcoinCoreGuixSubstitutes.enable`
- `services.bitcoinCoreGuixSubstitutes.domain`
- `services.bitcoinCoreGuixSubstitutes.dataDir`
- `services.bitcoinCoreGuixSubstitutes.storeDir`
- `services.bitcoinCoreGuixSubstitutes.publishAddress`
- `services.bitcoinCoreGuixSubstitutes.publishPort`
- `services.bitcoinCoreGuixSubstitutes.publishWorkers`
- `services.bitcoinCoreGuixSubstitutes.signingKey.*`

Builder options:

- `services.bitcoinCoreGuixSubstitutes.builder.enable`
- `services.bitcoinCoreGuixSubstitutes.builder.buildUser`
- `services.bitcoinCoreGuixSubstitutes.builder.buildGroup`
- `services.bitcoinCoreGuixSubstitutes.builder.bitcoinRepository`
- `services.bitcoinCoreGuixSubstitutes.builder.bitcoinBranch`
- `services.bitcoinCoreGuixSubstitutes.builder.buildJobs`
- `services.bitcoinCoreGuixSubstitutes.builder.nativeSystems`
- `services.bitcoinCoreGuixSubstitutes.builder.targetHosts`
- `services.bitcoinCoreGuixSubstitutes.builder.timeMachineUrl`
- `services.bitcoinCoreGuixSubstitutes.builder.timeMachineCommit`
- `services.bitcoinCoreGuixSubstitutes.builder.additionalTimeMachineFlags`
- `services.bitcoinCoreGuixSubstitutes.builder.schedule.*`
- `services.bitcoinCoreGuixSubstitutes.builder.retention.*`

The builder materializes each queued source job as immutable profiles, roots
profile derivations, materializes the complete derivation-output closure, then
waits for the local `guix publish` endpoint to serve all resulting substitutes.
