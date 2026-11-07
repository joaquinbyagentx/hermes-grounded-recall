#!/usr/bin/env python3
"""Launch the real Hermes CLI with demo-only state and no dotenv fallback.

This is an isolation shim, not an alternate agent, model, or recall implementation.
Python audit hooks are a tripwire, not an OS security sandbox for hostile plugins.
"""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys

sys.dont_write_bytecode = True
DEMO = Path(__file__).resolve().parent
ROOT = (DEMO / ".eval-home").resolve()


def inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def install_guard(home: Path) -> None:
    """Fail closed on Python file mutations outside this invocation's home."""
    def resolve(value):
        if isinstance(value, int) or value is None:
            return None  # already-open fd, not a new path
        return Path(os.fsdecode(value)).resolve()

    def check_write(value):
        path = resolve(value)
        if path is not None and not inside(path, home) and path != Path(os.devnull):
            raise PermissionError("Demo isolation blocked write outside temporary home: " + str(path))

    def audit(event, args):
        if event == "open":
            path, mode, flags = args
            resolved = resolve(path)
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if writing:
                check_write(path)
            elif resolved is not None and not inside(resolved, home) and (
                resolved.name in {".env", ".op.env", "auth.json", "credentials.json", "config.yaml"}
                or resolved.name.endswith(".env")
            ):
                raise PermissionError("Demo isolation blocked external config/credential read: " + str(resolved))
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.chown", "os.utime", "os.truncate"}:
            check_write(args[0])
        elif event in {"os.rename", "os.link"}:
            check_write(args[0])
            check_write(args[1])
        elif event == "os.symlink":
            check_write(args[1])
        elif event == "subprocess.Popen":
            executable, argv = args[:2]
            name = Path(os.fsdecode(executable)).name
            if name in {"uv", "pip", "pip3", "npm", "pnpm", "brew"} or (
                isinstance(argv, (list, tuple)) and "-m" in argv and "pip" in argv
            ):
                raise PermissionError("Demo isolation refuses dependency installation")
        elif event == "os.exec":
            raise PermissionError("Demo isolation refuses interpreter re-exec")

    sys.addaudithook(audit)


def main() -> int:
    raw_home = os.environ.get("HERMES_HOME", "")
    if not raw_home:
        raise RuntimeError("Explicit throwaway HERMES_HOME is required")
    home = Path(raw_home).resolve()
    if not inside(home, ROOT) or home == ROOT or not inside(Path.home().resolve(), home):
        raise RuntimeError("HOME and HERMES_HOME must both be inside demo/.eval-home/<run>/")
    install_guard(home)
    if len(sys.argv) < 2:
        raise RuntimeError("Expected cli, probe, or hook mode")
    mode, rest = sys.argv[1], sys.argv[2:]
    if mode == "hook":
        sys.argv = [str(DEMO / "run_eval.py"), "--_hook-worker", *rest]
        runpy.run_path(str(DEMO / "run_eval.py"), run_name="__main__")
    else:
        # Hermes loads PROJECT_ROOT/.env even with an isolated HERMES_HOME. Disable
        # dotenv hydration in THIS process only before importing its CLI entrypoint.
        from hermes_cli import env_loader
        env_loader.load_hermes_dotenv = lambda **_kwargs: []
        # The checkout otherwise prepares a fresh PM dependency generation for every
        # borrowed HERMES_HOME. Reuse only this explicitly selected, already prepared
        # interpreter. All changes here are local monkeypatches in this child.
        from hermes_cli import _early_recovery, venv_sync, boot_bootstrap
        from pm import environments
        _early_recovery.restore_interrupted_pull = lambda *_args, **_kwargs: False
        _early_recovery.recover_if_needed = lambda *_args, **_kwargs: None
        venv_sync.prepare_launch = lambda *_args, **_kwargs: None
        environments.activate_dependencies = lambda *_args, **_kwargs: None
        boot_bootstrap.maybe_run_boot_bootstrap = lambda *_args, **_kwargs: None
        if mode == "cli":
            sys.argv = ["hermes", *rest]
            from hermes_cli import main as hermes_main
            hermes_main._recover_update_debts_on_startup = lambda: None
            hermes_main._sweep_stale_bytecode_if_checkout_changed = lambda: None
            hermes_main.main()
        elif mode == "probe":
            import time
            from hermes_cli.gateway_runtime import discover_gateway_endpoint
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                result = discover_gateway_endpoint(home, timeout=1)
                if result.state == "ready":
                    return 0
                time.sleep(0.25)
            return 2
        else:
            raise RuntimeError("Unknown isolation mode: " + mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
