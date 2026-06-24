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
  };
}
