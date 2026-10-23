"""One request over stdin/stdout; no listening socket and no persistent worker."""
from __future__ import annotations

import contextlib
import fcntl
import json
from pathlib import Path
import sys
import traceback

# -I ignores PYTHONPATH and the current directory. Only this bundled package is added.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mlx_activation_lab import manager_operations


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("request too large")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        manager_operations.LOG_ROOT.mkdir(parents=True, exist_ok=True)
        # Also serialize workers from separate native processes during app handover.
        with (manager_operations.LOG_ROOT / "operations.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Launchers may print progress; stdout is reserved for one response envelope.
            with contextlib.redirect_stdout(sys.stderr):
                status, payload = manager_operations.dispatch(
                    request["method"], request["path"], request.get("payload", {}))
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        status, payload = 400, {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps({"status": status, "payload": payload}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
