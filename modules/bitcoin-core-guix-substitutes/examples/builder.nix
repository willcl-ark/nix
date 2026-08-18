{ ... }:
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
