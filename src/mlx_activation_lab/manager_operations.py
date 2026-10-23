"""Model operations used by the native server through its one-shot worker."""
from __future__ import annotations

from pathlib import Path
from . import server_control, manager_models

LOG_ROOT = manager_models.STATE_ROOT
LOG_PATH = LOG_ROOT / "mlx-server.log"


def recent_log() -> list[str]:
    path = LOG_PATH
    if server_control.status().get("format") == "GGUF":
        path = manager_models.GGUF_ROOT / "logs/llama-server.log"
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        handle.seek(0, 2)
        handle.seek(max(0, handle.tell() - 16_384))
        return handle.read().decode("utf-8", errors="replace").splitlines()[-35:]


def dispatch(method: str, path: str, payload: dict) -> tuple[int, dict]:
    if method == "GET":
        if path == "/api/cached-models":
            cached = server_control.cached_models()
            return (200, {"models": cached, "names": {model: manager_models.display_name(model) for model in cached}})
        if path == "/api/status":
            items = manager_models.models()
            return (200, {
                "app": "mlx-server-manager",
                "token": "",
                "server": server_control.status(),
                "aliases": server_control.aliases(),
                "models": items,
                "display_names": manager_models.settings().get("display_names", {}),
                "default_alias": manager_models.default_alias(items),
                "gguf_available": (manager_models.GGUF_ROOT / "server.py").is_file(),
                "gguf_restore_available": any(
                    profile["alias"] not in {item["alias"] for item in items}
                    and (manager_models.GGUF_ROOT / profile["manifest"]).is_file()
                    for profile in manager_models.gguf_profiles()),
                "log": recent_log(),
            })
        return 404, {"error": "not found"}
    if method != "POST":
        return 405, {"error": "method not allowed"}
    if not isinstance(payload, dict):
        raise ValueError("JSON objectを送ってください")
    if path == "/api/start":
        thinking = payload.get("thinking", False)
        if type(thinking) is not bool:
            raise ValueError("ThinkingはON/OFFで指定してください")
        result = {"pid": server_control.start(str(payload.get("alias", "")), LOG_PATH, thinking=thinking)}
    elif path == "/api/stop":
        pid = payload.get("pid")
        command = payload.get("command")
        if type(pid) is not int or not isinstance(command, str) or not command:
            raise ValueError("停止対象のPIDと起動コマンドを指定してください")
        server_control.stop(expected_pid=pid, expected_command=command)
        result = server_control.status()
    elif path == "/api/rename":
        manager_models.rename_model(str(payload.get("alias", "")), str(payload.get("name", "")))
        result = {}
    elif path == "/api/default":
        manager_models.set_default(str(payload.get("alias", "")))
        result = {"default_alias": manager_models.default_alias()}
    elif path == "/api/gguf/register":
        manager_models.restore_gguf()
        result = {}
    elif path in {"/api/delete-preview", "/api/delete"}:
        alias = str(payload.get("alias", ""))
        body = payload.get("body", False)
        if type(body) is not bool:
            raise ValueError("削除方法を確認してください")
        if "aliases" in payload:
            result = (manager_models.deletion_preview_many(payload["aliases"], body) if path.endswith("preview")
                      else manager_models.delete_models(payload["aliases"], body, str(payload.get("fingerprint", ""))))
        else:
            result = (manager_models.deletion_preview(alias, body) if path.endswith("preview")
                      else manager_models.delete_model(alias, body, str(payload.get("fingerprint", ""))))
    elif path == "/api/register":
        if payload.get("alias"):
            server_control.register_alias(str(payload["alias"]), str(payload.get("model", "")))
        else:
            model = str(payload.get("model", "")).strip()
            if model not in server_control.cached_models():
                raise ValueError("保存済みモデルが見つかりません。一覧を更新してください")
            server_control.register_model(model)
        result = {"aliases": server_control.aliases()}
    else:
        return 404, {"error": "not found"}
    return 200, {"ok": True, "result": result}
