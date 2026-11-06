"""Dispatch persisted review claims to independent systemd job services."""

import json
import fcntl
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess


INSTANCE = re.compile(r"[1-9][0-9]*-[0-9a-f]{32}")
LIVE_STATES = {"activating", "active", "reloading", "deactivating"}


def instance_name(job):
    token = job.get("attempt_token", job["token"])
    instance = f"{job['id']}-{token}"
    if not INSTANCE.fullmatch(instance):
        raise ValueError("invalid job instance")
    return instance


def unit_state(unit):
    result = subprocess.run(
        ["systemctl", "show", unit, "--property=LoadState,ActiveState,SubState,Job"],
        capture_output=True,
        text=True,
        timeout=3,
    )
    values = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    if values.get("LoadState") == "not-found":
        return "absent"
    if result.returncode or values.get("LoadState") != "loaded":
        raise RuntimeError(f"Cannot determine job unit state: {unit}")
    state = values.get("ActiveState")
    if values.get("Job") not in {None, "", "0"} or state in LIVE_STATES:
        return "live"
    if state in {"inactive", "failed"}:
        return "stopped"
    raise RuntimeError(f"Unknown job unit state: {unit}")


def store_paths(value):
    """Retain separately configured shells/profiles as well as the package."""
    if isinstance(value, str):
        match = re.match(r"(/nix/store/[^/]+)(?:/|$)", value)
        return {match[1]} if match else set()
    if isinstance(value, dict):
        return set().union(*(store_paths(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(store_paths(item) for item in value))
    return set()


class JobUnits:
    def __init__(
        self, jobs, state_dir, entries, key_file, report_dir=None, report_base_url=None
    ):
        self.jobs = jobs
        self.root = Path(state_dir)
        self.attempts = self.root / "attempts"
        self.attempts.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.entries = entries
        self.key_file = str(key_file)
        self.report_dir = str(report_dir) if report_dir is not None else None
        self.report_base_url = report_base_url
        self.executable = os.environ["RALPH_JOB_EXECUTABLE"]
        if not re.fullmatch(r"/nix/store/[^/]+/bin/ralph-job", self.executable):
            raise ValueError("RALPH_JOB_EXECUTABLE must name a pinned package")

    def prepare(self, job):
        directory = self.attempts / instance_name(job)
        directory.mkdir(mode=0o700, exist_ok=True)
        (directory / "execution.lock").touch(mode=0o600)
        manifest = {
            "version": 1,
            "job_id": job["id"],
            "token": job["token"],
            "repo_id": job["repo_id"],
            "executable": self.executable,
            "package": str(Path(self.executable).parent.parent),
            "state_dir": str(self.root),
            "entry": self.entries[job["repo_id"]],
            "openai_key_file": self.key_file,
            "report_dir": self.report_dir,
            "report_base_url": self.report_base_url,
        }
        # Register every immutable dependency before systemd can start the job.
        for index, package in enumerate(sorted(store_paths(manifest))):
            subprocess.run(
                [
                    "nix-store",
                    "--realise",
                    package,
                    "--add-root",
                    str(directory / f"root-{index}"),
                    "--indirect",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
        temporary = directory / "manifest.tmp"
        with temporary.open("x") as stream:
            os.chmod(temporary, 0o600)
            json.dump(manifest, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(directory / "manifest.json")
        return directory

    def start(self, job):
        directory = self.prepare(job)
        pending = directory / "launch-pending"
        pending.touch(mode=0o600)
        subprocess.run(
            [
                "systemctl",
                "start",
                "--no-block",
                f"ralph-job@{instance_name(job)}.service",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        pending.unlink()

    def reconcile(self):
        # Query everything first. A broken system bus must not trigger recovery.
        attempts = self.jobs.active_attempts()
        states = [(job, unit_state(job["unit_name"])) for job in attempts]
        for job, state in states:
            if state == "live":
                continue
            directory = self.attempts / instance_name(job)
            if (directory / "launch-pending").exists():
                # A lost StartUnit acknowledgement must not become permission
                # to launch another attempt. Stop cancels any outstanding start.
                stopped = subprocess.run(
                    ["systemctl", "stop", job["unit_name"]],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                state = unit_state(job["unit_name"])
                if state == "live" or (stopped.returncode and state != "absent"):
                    raise RuntimeError("Uncertain review launch has not stopped")
            directory.mkdir(mode=0o700, exist_ok=True)
            with (directory / "execution.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    # Start authorization can complete after a stale unit query.
                    # The worker holds this lock before validating its claim.
                    continue
                if self.jobs.release_attempt(job["id"], job["attempt_token"]):
                    shutil.rmtree(directory)

        owned = {instance_name(job) for job in self.jobs.active_attempts()}
        for directory in self.attempts.iterdir():
            if (
                not directory.is_dir()
                or not INSTANCE.fullmatch(directory.name)
                or directory.name in owned
            ):
                continue
            if unit_state(f"ralph-job@{directory.name}.service") != "live":
                shutil.rmtree(directory)

    def serve(self, stop):
        while not stop.is_set():
            try:
                self.reconcile()
                if stop.is_set():
                    break
                job = self.jobs.claim()
                if job is not None:
                    # An interrupted launch leaves ownership intact. Reconciliation
                    # observes the exact unit before allowing another attempt.
                    self.start(job)
                    continue
            except Exception:
                logging.exception("Could not dispatch or reconcile review units")
            stop.wait(1)
