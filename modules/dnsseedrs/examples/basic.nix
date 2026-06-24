{
  services.dnsseedrs.mainnet = {
    enable = true;
    chain = "main";
    seedDomain = "seed.example.org";
    serverName = "ns.example.org";
    soaRname = "admin.example.org";
    seedNodes = [ "1.2.3.4:8333" ];
    bind = [
      "udp://127.0.0.1:5353"
      "tcp://127.0.0.1:5353"
    ];
  };
}
