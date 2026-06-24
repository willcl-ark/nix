{ ... }:
{
  services.radicleMirror = {
    enable = true;
    domain = "radicle.example.org";

    seed = {
      enable = true;
      privateKeyFile = "/run/secrets/radicle-private-key";
      publicKey = "ssh-ed25519 ... radicle";
      nodeId = "z6...";
    };

    frontend.enable = true;

    bitcoinMirror = {
      enable = true;
      dataDir = "/var/lib/radicle-mirror";
    };
  };
}
