# Bitcoin Core Guix Substitutes NixOS Module

Reusable NixOS module for publishing Bitcoin Core Guix build substitutes.

The base module owns the Guix daemon, `guix publish`, public signing-key files,
the landing page, and Caddy virtual host wiring. The optional builder owns the
immutable manifest queue, nightly manifest submission, manifest worker, and
retention timers.

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

The builder installs `guix-bitcoin-submit` as an operator tool. Manual jobs are
submitted from a staged directory containing `manifests/manifest_*.scm` and an
optional regular-file-only `context/` directory.

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
- `services.bitcoinCoreGuixSubstitutes.builder.bitcoinRemote`
- `services.bitcoinCoreGuixSubstitutes.builder.bitcoinBranch`
- `services.bitcoinCoreGuixSubstitutes.builder.buildJobs`
- `services.bitcoinCoreGuixSubstitutes.builder.nativeSystems`
- `services.bitcoinCoreGuixSubstitutes.builder.targetHosts`
- `services.bitcoinCoreGuixSubstitutes.builder.timeMachineUrl`
- `services.bitcoinCoreGuixSubstitutes.builder.timeMachineCommit`
- `services.bitcoinCoreGuixSubstitutes.builder.additionalTimeMachineFlags`
- `services.bitcoinCoreGuixSubstitutes.builder.schedule.*`
- `services.bitcoinCoreGuixSubstitutes.builder.retention.*`

The builder is a hard cutover from the old mutable-checkout `guix-build` timer.
It materializes each queued manifest as immutable jobs, roots profile
derivations, materializes the complete derivation-output closure, then waits for
the local `guix publish` endpoint to serve all resulting substitutes.
