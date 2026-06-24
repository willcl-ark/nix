{ ... }:
{
  services.bitcoinDnsSeed = {
    enable = true;
    serverName = "ns.example.org";
    soaRname = "admin.example.org";

    mainnet = {
      enable = true;
      seedDomain = "seed.bitcoin.example.org";
      dnssecKeyFiles = [
        ./secrets/mainnet/Kseed.example.+013+12345.key
        ./secrets/mainnet/Kseed.example.+013+12345.private
      ];
    };
  };
}
