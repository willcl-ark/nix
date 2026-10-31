"""Disposable, offline experiments against pinned source revisions."""

import ctypes
import difflib
import fcntl
import hashlib
import json
import os
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

if __package__:
    from .repository import object_git_command

STAGE_SECONDS = 1800
OUTPUT_BYTES = 32_768
PATCH_BYTES = 65_536
SOURCE_BYTES = 512 * 1024 * 1024
FILE_BYTES = 1024 * 1024
WORK_BYTES = 8 * 1024 * 1024 * 1024

TOOLS = [
    {
        "type": "function",
        "strict": True,
        "name": "experiment_run",
        "description": (
            "Run a complete experiment in a fresh disposable source copy. All edits, "
            "build steps and tests must be in this single shell script: changes DO NOT "
            "persist between calls. side=head tests the reviewed commit; side=base "
            "tests its pinned comparison revision. The Bix build environment is ready. "
            "Put build and runtime artifacts under /tmp (e.g. cmake -B /tmp/build), "
            "so source patches contain tests and code rather than build output. "
            "Limits: 8 GiB aggregate memory, 128 processes, two CPUs, and a "
            "30-minute total stage deadline. Network access is disabled; loopback "
            "is available. Output and patches "
            "are bounded. Build, environment and timeout failures are inconclusive."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
                "side": {"type": "string", "enum": ["head", "base"]},
                "command": {"type": "string"},
            },
            "required": ["finding_id", "side", "command"],
            "additionalProperties": False,
        },
    }
]


def _kill(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


class ExperimentSession:
    def __init__(
        self, snapshot, finding_ids, *, check_cancelled=None, check_current=None
    ):
        self.snapshot = snapshot
        self.finding_ids = set(finding_ids)
        self.check_cancelled = check_cancelled
        self.check_current = check_current
        self.deadline = time.monotonic() + STAGE_SECONDS
        self.record = {
            "head_sha": snapshot.head_sha,
            "diff_base_sha": snapshot.diff_base_sha,
            "commands": [],
            "errors": [],
            "fresh_workspace_per_command": True,
        }
        self.directory = None
        self.lock = None
        self.unavailable = None
        self.archives = {}

    def _stopped(self):
        if self.check_cancelled is not None:
            self.check_cancelled()
        if time.monotonic() >= self.deadline:
            return "timeout"
        return None

    def __enter__(self):
        try:
            self.cgroup_root = Path(os.environ["RALPH_WRITE_VERIFIER_CGROUP"]).resolve(
                strict=True
            )
            if not self.cgroup_root.is_relative_to("/sys/fs/cgroup"):
                raise RuntimeError(
                    "experiment cgroup must be delegated under /sys/fs/cgroup"
                )
            self.launcher = os.environ["RALPH_WRITE_VERIFIER_SHELL"]
            Path(self.launcher).resolve(strict=True)
            self.bwrap = os.environ.get("RALPH_WRITE_VERIFIER_BWRAP") or shutil.which(
                "bwrap"
            )
            self.prlimit = shutil.which("prlimit")
            self.bash = str(Path(shutil.which("bash")).resolve(strict=True))
            self.python = str(Path(sys.executable).resolve(strict=True))
            if not self.bwrap or not self.prlimit:
                raise RuntimeError("bubblewrap and prlimit are required")
            if not all(
                path.startswith("/nix/store/")
                for path in (self.launcher, self.bash, self.python)
            ):
                raise RuntimeError(
                    "experiment shell, Python and Bash must be in /nix/store"
                )
            self.record["environment"] = self.launcher
        except (KeyError, OSError, TypeError, RuntimeError) as error:
            self.unavailable = str(error)
            self.record["errors"].append(self.unavailable)
        return self

    def _prepare(self):
        if self.directory is not None:
            return
        self.directory = tempfile.TemporaryDirectory(prefix="ralph-experiment-")
        self.lock = os.open(
            Path(tempfile.gettempdir()) / f"ralph-experiment-{os.getuid()}.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        while True:
            stopped = self._stopped()
            if stopped:
                raise RuntimeError(stopped)
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                time.sleep(0.1)

    def __exit__(self, *_):
        if self.directory:
            self.directory.cleanup()
        if self.lock is not None:
            os.close(self.lock)

    def _run(self, argv, *, limit, sink=None, env=None):
        """Drain output without allowing a child to fill controller RAM or disk."""
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if sink is not None else subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env or {"PATH": os.defpath, "LANG": "C.UTF-8"},
        )
        output = bytearray()
        total = 0
        status = "completed"
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    stopped = self._stopped()
                    if stopped:
                        status = stopped
                        _kill(process)
                        break
                    for key, _ in selector.select(0.1):
                        chunk = os.read(key.fileobj.fileno(), 65_536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        available = max(0, limit - total)
                        if sink is not None:
                            sink.write(chunk[:available])
                        else:
                            output.extend(chunk[:available])
                        total += len(chunk)
                        if total > limit:
                            status = "error"
                            _kill(process)
                            break
                    if status != "completed":
                        break
                while process.poll() is None:
                    stopped = self._stopped()
                    if stopped:
                        status = stopped
                        _kill(process)
                        break
                    time.sleep(0.1)
        finally:
            if process.poll() is None:
                _kill(process)
            process.stdout.close()
        return status, process.returncode, bytes(output), total > limit

    def _run_isolated(self, argv, entry):
        # The service controller lives in a sibling subgroup. Only the launcher
        # and its descendants enter this group, before any untrusted code runs.
        (self.cgroup_root / "cgroup.subtree_control").write_text("+memory +pids +cpu")
        group = Path(tempfile.mkdtemp(prefix="experiment-", dir=self.cgroup_root))
        try:
            for name, value in {
                "memory.max": str(WORK_BYTES),
                "memory.swap.max": "0",
                "memory.oom.group": "1",
                "pids.max": "128",
                "cpu.max": "200000 100000",
            }.items():
                (group / name).write_text(value)
            return self._run(
                [
                    self.python,
                    "-I",
                    str(Path(__file__).resolve()),
                    "--cgroup-exec",
                    str(group),
                    *argv,
                ],
                limit=6 * (OUTPUT_BYTES + PATCH_BYTES),
            )
        finally:
            # Also catches cancellation and startup failures. cgroup.kill includes
            # descendants that escaped their original process group/session.
            (group / "cgroup.kill").write_text("1")
            until = time.monotonic() + 5
            while "populated 1" in (group / "cgroup.events").read_text():
                if time.monotonic() >= until:
                    self.unavailable = (
                        "experiment cgroup did not empty after termination"
                    )
                    raise RuntimeError(self.unavailable)
                time.sleep(0.01)
            entry["resource_events"] = {
                name: {
                    key: int(value)
                    for key, value in (
                        line.split()
                        for line in (group / f"{name}.events").read_text().splitlines()
                    )
                }
                for name in ("memory", "pids")
            }
            group.rmdir()

    def _archive(self, side):
        if side not in self.archives:
            revision = (
                self.snapshot.head_sha
                if side == "head"
                else self.snapshot.diff_base_sha
            )
            target = Path(self.directory.name) / f"{side}.tar"
            with object_git_command(
                self.snapshot.checkout, ["archive", "--format=tar", revision], revision
            ) as (command, env):
                # Export attributes must not omit or rewrite pinned source bytes.
                (Path(env["GIT_DIR"]) / "info").mkdir(exist_ok=True)
                (Path(env["GIT_DIR"]) / "info" / "attributes").write_text(
                    "* -export-ignore -export-subst\n"
                )
                with target.open("wb") as sink:
                    status, code, _, truncated = self._run(
                        command, limit=SOURCE_BYTES, sink=sink, env=env
                    )
            if status != "completed" or code or truncated:
                raise RuntimeError(f"source archive failed: {status}, exit {code}")
            self.archives[side] = target
        return self.archives[side]

    def dispatch(self, name, args):
        if name != "experiment_run":
            return json.dumps({"error": "Unknown experiment tool"})
        finding_id, side, command = (
            args.get(key) for key in ("finding_id", "side", "command")
        )
        if (
            not isinstance(finding_id, str)
            or finding_id not in self.finding_ids
            or side not in ("head", "base")
        ):
            return json.dumps({"error": "Unknown finding or comparison side"})
        if not isinstance(command, str) or not command.strip() or len(command) > 32_768:
            return json.dumps({"error": "command must contain 1..32768 characters"})
        entry = {
            "id": f"cmd-{len(self.record['commands']) + 1}",
            "finding_id": finding_id,
            "side": side,
            "revision": self.snapshot.head_sha
            if side == "head"
            else self.snapshot.diff_base_sha,
            "command": command,
            "status": "unavailable",
            "exit_code": None,
            "output": "",
            "patch": "",
            "output_truncated": False,
            "patch_truncated": False,
        }
        self.record["commands"].append(entry)
        try:
            if self.unavailable:
                raise RuntimeError(self.unavailable)
            stopped = self._stopped()
            if stopped:
                entry["status"] = stopped
                raise RuntimeError(stopped)
            if self.check_current is not None:
                self.check_current()
            self._prepare()
            archive = self._archive(side)
            # Only immutable inputs cross the namespace boundary. All writes go to
            # bounded tmpfs mounts, including files created outside the worktree.
            argv = [
                self.prlimit,
                f"--as={WORK_BYTES}",
                f"--cpu={STAGE_SECONDS}",
                f"--fsize={WORK_BYTES}",
                "--nofile=1024",
                "--",
                self.bwrap,
                "--unshare-user",
                "--unshare-pid",
                "--unshare-net",
                "--unshare-ipc",
                "--unshare-uts",
                "--die-with-parent",
                "--as-pid-1",
                "--new-session",
                "--cap-drop",
                "ALL",
                "--clearenv",
                "--ro-bind",
                "/nix/store",
                "/nix/store",
                "--ro-bind",
                str(archive),
                "/source.tar",
                "--ro-bind",
                str(Path(__file__).resolve()),
                "/experiment.py",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--size",
                str(WORK_BYTES),
                "--tmpfs",
                "/work",
                "--size",
                str(WORK_BYTES),
                "--tmpfs",
                "/tmp",
                "--dir",
                "/tmp/home",
                "--chdir",
                "/work",
                "--remount-ro",
                "/",
                "--remount-ro",
                "/dev",
                "--setenv",
                "HOME",
                "/tmp/home",
                "--setenv",
                "TMPDIR",
                "/tmp",
                "--setenv",
                "NIX_BUILD_TOP",
                "/tmp",
                "--setenv",
                "LANG",
                "C.UTF-8",
                self.python,
                "-I",
                "/experiment.py",
                self.launcher,
                self.bash,
                command,
            ]
            status, code, output, truncated = self._run_isolated(argv, entry)
            if self.check_current is not None:
                self.check_current()
            events = entry["resource_events"]
            if (
                events["memory"].get("oom", 0)
                or events["memory"].get("oom_kill", 0)
                or events["pids"].get("max", 0)
            ):
                status = "resource_limit"
            entry.update(status=status, exit_code=code)
            if status != "completed":
                entry.update(
                    output=output.decode("utf-8", "replace"), output_truncated=truncated
                )
            else:
                entry["output"] = output.decode("utf-8", "replace")
                result = json.loads(output)
                if result.get("exit_code") != code or not result.get(
                    "capture_complete"
                ):
                    raise RuntimeError("sandbox evidence capture did not complete")
                entry.update(result)
                entry["status"] = "completed"
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            if entry["status"] == "completed":
                entry["status"] = "error"
            entry["error"] = str(error)
        return json.dumps(entry)


def _capture_patch(archive):
    """Compare with immutable archive bytes after all experiment children exit."""
    chunks = []
    used = 0
    truncated = False
    tracked = set()

    def append_patch(patch):
        nonlocal used, truncated
        data = patch.encode("utf-8")
        chunks.append(data[: max(0, PATCH_BYTES - used)])
        used += len(data)
        truncated |= used > PATCH_BYTES

    def append_diff(name, original, current):
        if original == current:
            return
        if b"\0" in original or b"\0" in current:
            patch = f"Binary file changed: {name}\n"
        else:
            patch = "".join(
                difflib.unified_diff(
                    original.decode("utf-8", "replace").splitlines(keepends=True),
                    current.decode("utf-8", "replace").splitlines(keepends=True),
                    fromfile=f"a/{name}",
                    tofile=f"b/{name}",
                )
            )
        append_patch(patch)

    def read_stream(stream):
        digest = hashlib.sha256()
        prefix = bytearray()
        total = 0
        while chunk := stream.read(65_536):
            digest.update(chunk)
            prefix.extend(chunk[: max(0, FILE_BYTES - total)])
            total += len(chunk)
        return bytes(prefix), digest.digest(), total > FILE_BYTES

    def read_file(path):
        try:
            for ancestor in path.parents:
                if ancestor == Path("/work"):
                    break
                if ancestor.is_symlink():
                    data = b"[parent directory replaced by symlink]\n"
                    return data, hashlib.sha256(data).digest(), False, "000000"
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode):
                with path.open("rb") as stream:
                    return (
                        *read_stream(stream),
                        "100755" if mode & 0o111 else "100644",
                    )
            data = (
                ("symlink: " + os.readlink(path) + "\n").encode()
                if stat.S_ISLNK(mode)
                else b"[not a regular file]\n"
            )
            file_mode = "120000" if stat.S_ISLNK(mode) else "040000"
        except FileNotFoundError:
            data, file_mode = b"", "000000"
        return data, hashlib.sha256(data).digest(), False, file_mode

    for member in archive.getmembers():
        if not member.isfile() and not member.issym():
            continue
        name = member.name
        tracked.add(name)
        if used >= PATCH_BYTES:
            truncated = True
            continue
        if member.issym():
            original = ("symlink: " + member.linkname + "\n").encode()
            old_digest, old_truncated = hashlib.sha256(original).digest(), False
        else:
            with archive.extractfile(member) as stream:
                original, old_digest, old_truncated = read_stream(stream)
        current, new_digest, new_truncated, new_mode = read_file(Path("/work") / name)
        old_mode = (
            "120000"
            if member.issym()
            else ("100755" if member.mode & 0o111 else "100644")
        )
        if old_mode != new_mode:
            append_patch(
                f"diff --git a/{name} b/{name}\nold mode {old_mode}\nnew mode {new_mode}\n"
            )
        if old_digest != new_digest:
            truncated |= old_truncated or new_truncated
            if original == current:
                append_diff(name, b"", b"[file differs beyond capture limit]\n")
            else:
                append_diff(name, original, current)
    examined = 0
    for root, dirs, files in os.walk("/work", followlinks=False):
        dirs[:] = sorted(name for name in dirs if name != ".git")
        files.extend(name for name in dirs if (Path(root) / name).is_symlink())
        for filename in sorted(files):
            path = Path(root) / filename
            name = str(path.relative_to("/work"))
            if name in tracked:
                continue
            examined += 1
            if examined > 5000 or used >= PATCH_BYTES:
                truncated = True
                break
            current, _, incomplete, file_mode = read_file(path)
            append_patch(f"diff --git a/{name} b/{name}\nnew file mode {file_mode}\n")
            truncated |= incomplete
            append_diff(name, b"", current)
        if examined > 5000 or used >= PATCH_BYTES:
            break
    return b"".join(chunks).decode("utf-8", "replace"), truncated


def _sandbox_main():
    # Prevent experiment code from reading/writing this supervisor's memory or
    # file descriptors through /proc. Failure must not weaken evidence capture.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) or libc.prctl(36, 1, 0, 0, 0):
        raise RuntimeError("cannot protect experiment supervisor")
    with tarfile.open("/source.tar") as archive:
        archive.extractall("/work", filter="data")
        process = subprocess.Popen(
            [sys.argv[1], sys.argv[2], "-c", sys.argv[3]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        output = bytearray()
        total = 0
        # Read until the command exits, even if an orphan retains its output pipe.
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while process.poll() is None or selector.get_map():
                events = selector.select(0.1)
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 65_536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk[: max(0, OUTPUT_BYTES - total)])
                    total += len(chunk)
                if process.poll() is not None and not events:
                    break
        code = process.wait()
        if code < 0:
            code = 128 - code
        process.stdout.close()
        # Descendants can create new sessions, so kill every other process in
        # this private PID namespace before inspecting any experiment files.
        while True:
            others = [
                int(item.name)
                for item in Path("/proc").iterdir()
                if item.name.isdigit() and int(item.name) not in (1, os.getpid())
            ]
            if not others:
                break
            for pid in others:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                while os.waitpid(-1, os.WNOHANG)[0]:
                    pass
            except ChildProcessError:
                pass
            time.sleep(0.01)
        patch, truncated = _capture_patch(archive)
    print(
        json.dumps(
            {
                "exit_code": code,
                "output": output.decode("utf-8", "replace"),
                "output_truncated": total > OUTPUT_BYTES,
                "patch": patch,
                "patch_truncated": truncated,
                "capture_complete": True,
            }
        )
    )
    return code


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cgroup-exec":
        (Path(sys.argv[2]) / "cgroup.procs").write_text(str(os.getpid()))
        os.execv(sys.argv[3], sys.argv[3:])
    raise SystemExit(_sandbox_main())
