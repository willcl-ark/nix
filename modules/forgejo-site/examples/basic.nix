{ ... }:
{
  services.forgejoSite = {
    enable = true;
    domain = "code.example.org";

    admin = {
      user = "admin";
      email = "admin@example.org";
    };
  };
}
