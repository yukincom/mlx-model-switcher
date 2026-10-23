from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path


SERVER_ROOT = Path(os.environ.get("MLX_ACTIVATION_LAB_SERVER_ROOT", "~/mlx-server")).expanduser()
START_SCRIPT = SERVER_ROOT / "start_server.sh"
MODELS_CONFIG = SERVER_ROOT / "models.conf"
PORT = 8080


def aliases() -> list[dict[str, str]]:
    result = []
    if not MODELS_CONFIG.is_file():
        return result
    for raw in MODELS_CONFIG.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        alias, model = [part.strip() for part in line.split("=", 1)]
        if alias and model:
            result.append({"alias": alias, "model": model})
    return result


def register_alias(alias: str, model: str) -> None:
    alias = re.sub(r"[^A-Za-z0-9._-]+", "-", alias.strip()).strip("-.")
    if not alias:
        raise ValueError("エイリアス名が空です")
    model = model.strip()
    if not model:
        raise ValueError("モデルのIDまたはパスが空です")
    if "\n" in model or "\r" in model:
        raise ValueError("モデルのIDまたはパスに改行は使えません")
    existing = aliases()
    if any(item["alias"] == alias and item["model"] != model for item in existing):
        raise ValueError(f"別のモデルが同じエイリアスを使っています: {alias}")
    if any(item["alias"] == alias and item["model"] == model for item in existing):
        return
    if not MODELS_CONFIG.is_file():
        raise ValueError(f"モデル設定が見つかりません: {MODELS_CONFIG}")
    with MODELS_CONFIG.open("a", encoding="utf-8") as handle:
        handle.write(f"\n{alias}={model}\n")


def cached_models(cache_dir: str | Path | None = None) -> list[str]:
    """List complete cached model references using the launcher's revision rules."""
    from huggingface_hub import scan_cache_dir
    from huggingface_hub.constants import HF_HUB_CACHE

    root = Path(cache_dir or HF_HUB_CACHE).expanduser()
    if not root.is_dir():
        return []
    models = []
    for repo in scan_cache_dir(root).repos:
        if repo.repo_type != "model":
            continue
        if (repo.repo_path / "refs/main").is_file():
            revision = repo.refs.get("main")
        else:
            revision = next(iter(repo.revisions)) if len(repo.revisions) == 1 else None
        if revision is None:
            continue
        snapshot = revision.snapshot_path
        try:
            config = json.loads((snapshot / "config.json").read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                continue
            architectures = config.get("architectures") or []
            if architectures and not any(
                isinstance(name, str) and name.endswith(("ForCausalLM", "ForConditionalGeneration"))
                for name in architectures
            ):
                continue
            complete = any(
                path.is_file() and path.stat().st_size > 0
                for path in snapshot.glob("*.safetensors")
            )
        except (OSError, json.JSONDecodeError):
            continue
        if complete:
            models.append(repo.repo_id)
    return sorted(models, key=str.casefold)


def register_model(model: str) -> str:
    """Keep launcher aliases internal; registration never downloads a model."""
    model = model.strip()
    existing = aliases()
    for item in existing:
        if item["model"] == model:
            return item["alias"]
    basename = Path(model.rstrip("/")).name
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", basename).strip("-.") or "model"
    used = {item["alias"] for item in existing}
    alias = base
    suffix = 2
    while alias in used:
        alias = f"{base}-{suffix}"
        suffix += 1
    register_alias(alias, model)
    return alias


def _listener_pid(port: int = PORT, *, strict: bool = False) -> int | None:
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5,
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        values = sorted(set(lines))
        if (result.returncode not in (0, 1) or result.stderr.strip()
                or any(not value.isdigit() for value in values) or len(values) > 1):
            raise ValueError("8080の使用状況を確認できません")
        return int(values[0]) if values else None
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        if strict:
            raise RuntimeError("8080の使用状況を確認できないため、操作しません") from exc
        return None


def _command(pid: int) -> str:
    try:
        return subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _trusted(command: str) -> bool:
    if not command:
        return False
    trusted_fragments = [
        str(SERVER_ROOT / ".venv/bin/python"),
        str(SERVER_ROOT / "local_server_entry"),
        "mlx_priority_server.py",
        "mlx_lm.server",
    ]
    return any(fragment in command for fragment in trusted_fragments) and "--port 8080" in command


def _active_model(command: str) -> str:
    match = re.search(r"(?:^|\s)--model\s+(.+?)(?=\s+--?[A-Za-z]|$)", command)
    if not match and "llama-server" in command:
        match = re.search(r"(?:^|\s)-m\s+(.+?)(?=\s+--?[A-Za-z]|$)", command)
    return match.group(1).strip("'\"") if match else ""


def _thinking_from_command(command: str) -> bool | None:
    """Read the launch default, without claiming to know per-request overrides."""
    reasoning = re.search(r"(?:^|\s)(?:--reasoning|-rea)(?:=|\s+)(on|off)(?=\s|$)", command)
    if reasoning:
        return reasoning.group(1) == "on"
    template = re.search(r"(?:^|\s)--chat-template-args(?:=|\s+)(.+?)(?=\s+--?[A-Za-z]|$)", command)
    if template:
        try:
            value = json.loads(template.group(1).strip().strip("'"))
            thinking = value.get("enable_thinking") if isinstance(value, dict) else None
            return thinking if type(thinking) is bool else None
        except (ValueError, TypeError):
            pass
    return None


def status() -> dict:
    from . import manager_models
    current = _status()
    current.setdefault("thinking", _thinking_from_command(current.get("command", "")))
    current["names_version"] = manager_models.names_version()
    current["active_name"] = manager_models.display_name(current["active_model"]) if current.get("active_model") else ""
    return current


def _status() -> dict:
    pid = _listener_pid()
    from . import manager_models
    if pid is None:
        pending = manager_models.pending_gguf() or manager_models.pending_mlx()
        if pending:
            return pending
        return {"running": False, "pid": None, "trusted": False, "command": "", "active_model": ""}
    command = _command(pid)
    current = {
        "running": True,
        "pid": pid,
        "trusted": _trusted(command),
        "command": command,
        "active_model": _active_model(command),
        "format": "GGUF" if "llama-server" in command else "MLX",
    }
    managed_model = manager_models.managed_gguf(current)
    if managed_model:
        current.update(trusted=True, active_model=managed_model, format="GGUF")
    return current


def start(alias: str, log_path: str | Path, *, thinking: bool = False) -> int:
    if type(thinking) is not bool:
        raise ValueError("ThinkingはON/OFFで指定してください")
    if status()["running"] or _listener_pid(strict=True) is not None:
        raise ValueError("8080はすでに稼働中です。切り替える場合は先に停止してください")
    from . import manager_models
    if any(item["alias"] == alias for item in manager_models.gguf_profiles()):
        return manager_models.start_gguf(alias, thinking=thinking)
    if alias not in {item["alias"] for item in aliases()}:
        raise ValueError(f"未登録のエイリアスです: {alias}")
    target = Path(log_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("ab") as log:
        proc = subprocess.Popen(
            [str(START_SCRIPT), alias], cwd=SERVER_ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, start_new_session=True,
            # The app manages this exact endpoint/configuration, regardless of inherited CLI options.
            env={**os.environ, "MLX_THINKING": "on" if thinking else "off",
                 "MLX_PORT": str(PORT), "MLX_HOST": "127.0.0.1", "MLX_ALLOW_REMOTE": "0",
                 "MLX_SERVER_DIR": str(SERVER_ROOT), "MLX_MODELS_CONFIG": str(MODELS_CONFIG),
                 "MLX_MANAGER_LOGGED": "1"},
        )
    time.sleep(0.25)
    if proc.poll() is not None:
        raise RuntimeError(f"MLXサーバーが起動直後に終了しました。ログ: {target}")
    manager_models.remember_start(proc.pid, alias, thinking=thinking)
    return proc.pid


def stop(timeout: float = 20.0, *, expected_pid: int | None = None, expected_command: str | None = None) -> None:
    current = status()
    if not current["running"]:
        return
    if expected_pid is not None and (current["pid"] != expected_pid or current["command"] != expected_command):
        raise RuntimeError("8080のプロセスが表示後に切り替わったため、停止しません。状態を確認し直してください")
    if not current["trusted"]:
        raise RuntimeError("8080のプロセスを既知のサーバーの起動記録と確認できないため、停止しません")
    pid = int(current["pid"])
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _listener_pid() != pid and not _command(pid):
            return
        time.sleep(0.2)
    raise RuntimeError("サーバーはまだ終了中です。強制終了は行いません")


def write_runtime_record(path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(status(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
