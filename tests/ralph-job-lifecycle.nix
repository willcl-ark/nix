{ pkgs }:
let
  fakePackage =
    version:
    pkgs.runCommand "ralph-job-fixture-${version}" { } ''
      mkdir -p $out/bin
      cat > $out/bin/ralph-service <<'PY'
      #!${pkgs.python3}/bin/python3
      import signal
      import time
      signal.signal(signal.SIGTERM, lambda *_: exit(0))
      while True:
          time.sleep(0.1)
      PY
      cat > $out/bin/ralph-job <<'PY'
      #!${pkgs.python3}/bin/python3
      import fcntl
      import json
      import os
      from pathlib import Path
      import sys
      import time

      manifest = Path(sys.argv[2])
      state = manifest.parent
      data = json.loads(manifest.read_text())
      execution_lock = (state / "execution.lock").open("a+")
      fcntl.flock(execution_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
      if "job_id" in data:
          from ralph_service.jobs import JobStore
          store = JobStore(Path(data["state_dir"]) / "jobs.sqlite3", initialize=False)
          job = store.get_claim(data["job_id"], data["token"])
          assert job is not None
      cgroup = Path(os.environ["RALPH_WRITE_VERIFIER_CGROUP"])
      (cgroup / "cgroup.subtree_control").write_text("+memory +pids +cpu")
      probe = cgroup / "probe"
      probe.mkdir()
      (probe / "memory.max").write_text("67108864")
      probe.rmdir()
      (state / "started.json").write_text(json.dumps({
          "pid": os.getpid(), "version": "${version}",
          "template": os.environ["TEST_TEMPLATE_VERSION"],
          "cgroup": str(cgroup),
          "lock": os.environ["RALPH_WRITE_VERIFIER_LOCK"],
      }))
      while not (state / "release").exists():
          time.sleep(0.1)
      (state / "published").write_text("${version}")
      if "job_id" in data:
          assert store.save_result(job, {"content": "${version}"})
          assert store.complete(job)
      PY
      chmod +x $out/bin/*
    '';
  pythonPath = "${../pkgs/ralph-service}:${../pkgs/ralph}";
  adapter = pkgs.writeScriptBin "ralph-test-dispatch" ''
    #!${pkgs.python3}/bin/python3
    import json
    import os
    from pathlib import Path
    import sys
    sys.path[:0] = "${pythonPath}".split(":")
    from ralph_service.jobs import JobStore
    from ralph_service.job_units import JobUnits, instance_name

    root = Path("/var/lib/ralph")
    jobs = JobStore(root / "jobs.sqlite3", global_limit=2, repository_limits={"core": 2}, retry_base_seconds=0)
    units = JobUnits(jobs, root, {"core": {"repo_id": "core"}}, root / "key")
    command = sys.argv[1]
    if command in {"start", "claim-only", "prepare-only"}:
        number = int(sys.argv[2])
        jobs.enqueue(number, "master", "a" * 40, "synchronize", engine_identity={"name": "fixture"})
        job = jobs.claim()
        assert job is not None
        if command == "start":
            units.start(job)
        elif command == "prepare-only":
            units.prepare(job)
        print(instance_name(job))
    elif command == "full":
        jobs.enqueue(99, "master", "b" * 40, "synchronize", engine_identity={"name": "fixture"})
        assert jobs.claim() is None
    elif command == "checkpoint":
        attempt = jobs.active_attempts()[0]
        job = jobs.get_claim(attempt["id"], attempt["attempt_token"])
        assert jobs.save_result(job, {"content": "saved-before-reboot"})
        print(job["id"])
    elif command == "saved":
        with jobs._connection() as db:
            result = db.execute("SELECT review_result FROM jobs WHERE id = ?", (int(sys.argv[2]),)).fetchone()[0]
        assert json.loads(result)["content"] == "saved-before-reboot"
    elif command == "reconcile":
        units.reconcile()
        print(len(jobs.active_attempts()))
    elif command == "retry":
        units.reconcile()
        job = jobs.claim()
        assert job is not None
        units.start(job)
        print(instance_name(job))
  '';
  dispatchCommand = pkgs.writeScript "ralph-test-command" ''
    #!${pkgs.python3}/bin/python3
    import json
    import os
    from pathlib import Path
    import subprocess
    command = json.loads(Path("/var/lib/ralph/test-command.json").read_text())
    result = subprocess.run(
        ["${adapter}/bin/ralph-test-dispatch", *command["args"]],
        env={**os.environ, "RALPH_JOB_EXECUTABLE": command["executable"]},
        text=True, capture_output=True,
    )
    Path("/var/lib/ralph/test-result").write_text(result.stdout)
    print(result.stderr, end="")
    raise SystemExit(result.returncode)
  '';
  oldPackage = fakePackage "old";
  newPackage = fakePackage "new";
in
pkgs.testers.runNixOSTest {
  name = "ralph-independent-job-lifecycle";
  nodes.machine = { config, lib, ... }: {
    imports = [ ../modules/ralph/nixos ];
    virtualisation.memorySize = 1024;
    environment.systemPackages = [
      adapter
      pkgs.nix
    ];
    systemd.services.ralph-test-dispatch = {
      path = [
        pkgs.nix
        pkgs.systemd
      ];
      serviceConfig = config.systemd.services.ralph.serviceConfig // {
        Type = "oneshot";
        ExecStart = dispatchCommand;
        Restart = "no";
      };
    };
    systemd.services."ralph-job@".environment.PYTHONPATH = pythonPath;
    services.ralph = {
      enable = true;
      package = oldPackage;
      openaiKeyFile = "/var/lib/ralph/key";
      repositories.core = {
        origin = "https://example.invalid/repo.git";
        repository = "owner/repo";
        botLogin = "ralph";
        forgejoApi = "https://example.invalid/api/v1/repos/owner/repo";
        webhookSecretFile = "/var/lib/ralph/key";
        forgejoTokenFile = "/var/lib/ralph/key";
      };
    };
    systemd.services."ralph-job@".environment.TEST_TEMPLATE_VERSION = "old";
    specialisation.updated.configuration = {
      services.ralph.package = lib.mkForce newPackage;
      systemd.services."ralph-job@".environment.TEST_TEMPLATE_VERSION = lib.mkForce "new";
    };
  };
  testScript = ''
    import json
    import time

    machine.start(allow_reboot=True)
    machine.wait_for_unit("ralph.service")
    machine.succeed("install -d -m 700 -o ralph -g ralph /var/lib/ralph/attempts")
    old = "1-" + "a" * 32
    new = "2-" + "b" * 32
    cancelled = "3-" + "c" * 32

    def prepare(instance, executable):
        directory = "/var/lib/ralph/attempts/" + instance
        machine.succeed("install -d -m 700 -o ralph -g ralph " + directory)
        manifest = json.dumps({"executable": executable})
        machine.succeed("printf '%s' '" + manifest + "' > " + directory + "/manifest.json")
        machine.succeed("chown ralph:ralph " + directory + "/manifest.json")
        return directory

    def manage(verb, instance):
        machine.succeed("su -s /bin/sh ralph -c 'systemctl " + verb + " ralph-job@" + instance + ".service'")

    def started(directory):
        machine.wait_until_succeeds("test -f " + directory + "/started.json", timeout=30)
        return json.loads(machine.succeed("cat " + directory + "/started.json"))

    old_dir = prepare(old, "${oldPackage}/bin/ralph-job")
    manage("start", old)
    old_state = started(old_dir)
    assert old_state["version"] == "old"
    assert old_state["template"] == "old"
    assert old_state["cgroup"] == "/sys/fs/cgroup/system.slice/ralph-job@" + old + ".service"
    assert old_state["lock"] == "/var/lib/ralph/experiment.lock"

    # Coordinator privileges allow only fixed worker instances, not arbitrary units.
    machine.fail("su -s /bin/sh ralph -c 'systemctl --no-ask-password stop sshd.service'")
    machine.fail("su -s /bin/sh ralph -c 'systemctl --no-ask-password start ralph-job@bad.service'")
    machine.fail("su -s /bin/sh ralph -c 'systemd-run --no-ask-password --unit=ralph-job-evil ${pkgs.coreutils}/bin/true'")

    machine.fail("su -s /bin/sh ralph -c 'systemd-run --no-ask-password --unit=ralph-job@4-" + "d" * 32 + ".service --property=User=root ${pkgs.coreutils}/bin/true'")

    before = time.monotonic()
    machine.succeed("systemctl restart ralph.service")
    assert time.monotonic() - before < 10
    assert machine.succeed("systemctl show -p MainPID --value ralph-job@" + old + ".service").strip() == str(old_state["pid"])

    # Change both the coordinator package and worker template while a review waits.
    before = time.monotonic()
    machine.succeed("/run/current-system/specialisation/updated/bin/switch-to-configuration test")
    assert time.monotonic() - before < 60
    machine.wait_for_unit("ralph.service")
    assert machine.succeed("systemctl show -p MainPID --value ralph-job@" + old + ".service").strip() == str(old_state["pid"])
    machine.succeed("test ! -e " + old_dir + "/published")

    new_dir = prepare(new, "${newPackage}/bin/ralph-job")
    manage("start", new)
    new_state = started(new_dir)
    assert new_state["version"] == "new"
    assert new_state["template"] == "new"
    assert old_state["pid"] != new_state["pid"]

    cancel_dir = prepare(cancelled, "${newPackage}/bin/ralph-job")
    manage("start", cancelled)
    started(cancel_dir)
    manage("stop", cancelled)
    machine.fail("systemctl is-active ralph-job@" + cancelled + ".service")
    machine.succeed("test ! -e " + cancel_dir + "/published")

    machine.succeed("touch " + old_dir + "/release " + new_dir + "/release")
    machine.wait_until_succeeds("test -f " + old_dir + "/published -a -f " + new_dir + "/published")
    assert machine.succeed("cat " + old_dir + "/published").strip() == "old"
    assert machine.succeed("cat " + new_dir + "/published").strip() == "new"
    machine.wait_until_fails("systemctl is-active ralph-job@" + old + ".service")
    machine.wait_until_fails("systemctl is-active ralph-job@" + new + ".service")

    # Run the production dispatcher and SQLite ownership logic as the service user.
    def dispatch(command, package="${newPackage}"):
        request = json.dumps({"args": command.split(), "executable": package + "/bin/ralph-job"})
        machine.succeed("printf '%s' '" + request + "' > /var/lib/ralph/test-command.json")
        machine.succeed("chown ralph:ralph /var/lib/ralph/test-command.json")
        machine.succeed("systemctl start ralph-test-dispatch.service")
        return machine.succeed("cat /var/lib/ralph/test-result").strip()

    first = dispatch("start 10", "${oldPackage}")
    first_dir = "/var/lib/ralph/attempts/" + first
    first_pid = started(first_dir)["pid"]
    second = dispatch("start 11")
    second_dir = "/var/lib/ralph/attempts/" + second
    started(second_dir)
    dispatch("full")
    machine.succeed("test -L " + first_dir + "/root-0")
    machine.succeed("nix-store --gc --print-roots | grep -F '" + first_dir + "/root-0'")
    machine.succeed("systemctl restart ralph.service")
    assert dispatch("reconcile") == "2"
    dispatch("full")
    assert machine.succeed("systemctl show -p MainPID --value ralph-job@" + first + ".service").strip() == str(first_pid)

    machine.succeed("touch " + first_dir + "/release " + second_dir + "/release")
    machine.wait_until_fails("systemctl is-active ralph-job@" + first + ".service")
    machine.wait_until_fails("systemctl is-active ralph-job@" + second + ".service")
    assert dispatch("reconcile") == "0"
    machine.succeed("test ! -e " + first_dir + " -a ! -e " + second_dir)

    # A coordinator dying after claim or preparation must not strand capacity.
    abandoned = dispatch("prepare-only 12")
    machine.succeed("test -f /var/lib/ralph/attempts/" + abandoned + "/manifest.json")
    assert dispatch("reconcile") == "0"
    machine.succeed("test ! -e /var/lib/ralph/attempts/" + abandoned)
    retry_instance = dispatch("retry")
    retry_dir = "/var/lib/ralph/attempts/" + retry_instance
    started(retry_dir)
    assert retry_instance != abandoned
    machine.succeed("touch " + retry_dir + "/release")
    machine.wait_until_fails("systemctl is-active ralph-job@" + retry_instance + ".service")
    assert dispatch("reconcile") == "0"

    interrupted = dispatch("start 13")
    interrupted_dir = "/var/lib/ralph/attempts/" + interrupted
    started(interrupted_dir)
    saved_job_id = dispatch("checkpoint")
    machine.reboot()
    machine.wait_for_unit("ralph.service")
    assert dispatch("reconcile") == "0"
    dispatch("saved " + saved_job_id)
    machine.succeed("test ! -e " + interrupted_dir)
    resumed = dispatch("retry")
    resumed_dir = "/var/lib/ralph/attempts/" + resumed
    started(resumed_dir)
    assert resumed != interrupted
    machine.succeed("touch " + resumed_dir + "/release")
    machine.wait_until_fails("systemctl is-active ralph-job@" + resumed + ".service")
    assert dispatch("reconcile") == "0"
  '';
}
