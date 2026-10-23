"""MLXと既存MoADのGGUFをモデル別に登録・表示する。起動とPID照合は既存ヘルパーへ委ねる。"""
from __future__ import annotations

import hashlib
import fcntl
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from . import server_control
from .model_store import delete_cached_model

STATE_ROOT = Path(os.environ.get("MLX_MANAGER_STATE_ROOT", Path.home() / "Library/Application Support/MLX Server Manager")).expanduser()
SETTINGS = STATE_ROOT / "models.json"
PENDING = STATE_ROOT / "starting.json"
# Optional, explicitly configured external launcher; never discover personal folders.
GGUF_ROOT = Path(os.environ.get("MLX_MANAGER_GGUF_ROOT", STATE_ROOT / "gguf")).expanduser()


def settings():
    return json.loads(SETTINGS.read_text()) if SETTINGS.is_file() else {}


def save_settings(value):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(SETTINGS)


def update_settings(change):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    with SETTINGS.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = settings()
        change(value)
        save_settings(value)


def names_version():
    return SETTINGS.stat().st_mtime_ns if SETTINGS.is_file() else 0


def display_name(model):
    profile = next((item for item in gguf_profiles() if item["model"] == model), None)
    return settings().get("display_names", {}).get(model) or (profile["name"] if profile else short_name(model))


def set_display_name(model, name):
    name = name.strip()
    if not name or len(name) > 60 or any(ord(char) < 32 for char in name):
        raise ValueError("表示名は1〜60文字で、改行せずに入力してください")
    update_settings(lambda value: value.setdefault("display_names", {}).__setitem__(model, name))


def gguf_runtime():
    source = GGUF_ROOT / "server.py"
    if not source.is_file():
        return None
    spec = importlib.util.spec_from_file_location("local_qwen_launcher", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def gguf_profiles():
    """起動ヘルパーを正本として、モデル別の名前・保存先・起動対象を読み取る。"""
    runtime = gguf_runtime()
    return runtime.gguf_profiles() if runtime is not None else []


def gguf_profile(alias):
    profile = next((item for item in gguf_profiles() if item["alias"] == alias), None)
    if profile is None:
        raise ValueError("GGUFモデルの起動設定が見つかりません")
    return profile


def gguf_files(alias):
    # 選んだモデルのmanifestだけを読む。別モデルの本体やMTPを削除対象に混ぜない。
    manifest = GGUF_ROOT / gguf_profile(alias)["manifest"]
    if not manifest.is_file():
        return []
    root = GGUF_ROOT / "models"
    files = []
    for item in json.loads(manifest.read_text())["files"]:
        path = root / item["directory"] / item["file"]
        # Reject traversal and every symlink, including symlinked ancestors.
        if not path.is_relative_to(root) or ".." in path.parts or path.suffix != ".gguf":
            raise ValueError("GGUFの保存先を確認できません")
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError("リンク先のモデル本体は削除対象にできません")
        if path.resolve().is_relative_to(root.resolve()) and path.is_file():
            files.append(path)
    return files


def _file_info(path):
    stat = path.stat()
    return [str(path), stat.st_size, stat.st_mtime_ns, stat.st_ino]


def _cache_repos():
    from huggingface_hub import scan_cache_dir
    from huggingface_hub.constants import HF_HUB_CACHE
    if not Path(HF_HUB_CACHE).is_dir():
        return []
    return [repo for repo in scan_cache_dir().repos if repo.repo_type == "model"]


def short_name(model):
    name = model.rsplit("/", 1)[-1]
    if "Flash-Next" in name:
        return "Qwen3.8-Flash-Next-125B"
    gemma = re.search(r"gemma[-_ ]?(\d+)[-_ ](E\d+B|\d+B)", name, re.I)
    qwen = re.search(r"Qwen(\d+(?:\.\d+)?)[-_ ](\d+B)", name, re.I)
    if gemma:
        label = f"Gemma-{gemma[1]}-{gemma[2].upper()}"
    elif qwen:
        label = f"Qwen{qwen[1]}-{qwen[2].upper()}"
    else:
        label = re.sub(r"[-_](?:it|mlx|qat|bf16|fp16|\d+bit)(?=[-_]|$)", "", name, flags=re.I)
        label = label.replace("_", "-")[:60]
    for variant in ("Huihui", "Josiefied", "AEON"):
        if variant.casefold() in name.casefold():
            label += f"-{variant}"
            break
    return label


def rename_model(alias, name):
    item = find_model(alias)
    set_display_name(item["model"], name)


def models():
    repos = {repo.repo_id: repo for repo in _cache_repos()}
    result = []
    for index, item in enumerate(server_control.aliases()):
        repo = repos.get(item["model"])
        # Repo directory creation time approximates acquisition order, without network access.
        path = repo.repo_path if repo else Path(item["model"]).expanduser()
        stat = path.stat() if path.exists() else None
        result.append({**item, "format": "MLX", "name": item["model"].split("/")[-1],
                       "size_bytes": int(repo.size_on_disk) if repo else None,
                       "acquired_at": getattr(stat, "st_birthtime", stat.st_ctime) if stat else None,
                       "order": index})
    value = settings()
    hidden = set(value.get("hidden_gguf_aliases", []))
    profiles = gguf_profiles()
    for profile in profiles:
        if profile["alias"] in hidden or not (GGUF_ROOT / profile["manifest"]).is_file():
            continue
        stats = [path.stat() for path in gguf_files(profile["alias"])]
        result.append({"alias": profile["alias"], "model": profile["model"],
                       "name": profile["name"], "format": "GGUF",
                       "size_bytes": sum(stat.st_size for stat in stats),
                       "acquired_at": max((getattr(stat, "st_birthtime", stat.st_ctime) for stat in stats), default=None),
                       "order": len(result)})
    names = value.get("display_names", {})
    for item in result:
        item["name"] = names.get(item["model"]) or (item["name"] if item["format"] == "GGUF" else short_name(item["model"]))
    return result


def default_alias(items=None):
    items = models() if items is None else items
    available = {item["alias"] for item in items}
    chosen = settings().get("default_alias", "")
    return chosen if chosen in available else next(iter(item["alias"] for item in items), "")


def find_model(alias):
    item = next((item for item in models() if item["alias"] == alias), None)
    if item is None:
        raise ValueError("モデルが未登録です。一覧を更新してください")
    return item


def set_default(alias):
    find_model(alias)
    update_settings(lambda value: value.update(default_alias=alias))


def restore_gguf():
    if not gguf_profiles():
        raise ValueError("GGUFの既存起動環境が見つかりません")
    update_settings(lambda value: value.update(hidden_gguf_aliases=[]))


def managed_gguf(current):
    try:
        runtime = gguf_runtime()
        if runtime is not None and runtime.is_our_llama(current):
            record = json.loads(runtime.RECORD.read_text())
            target = record["target"]
            profile = next((item for item in runtime.gguf_profiles() if item["target"] == target), None)
            return profile["model"] if profile else ""
    except (OSError, ValueError, KeyError):
        pass
    return ""


def pending_gguf():
    try:
        runtime = gguf_runtime()
        if runtime is None or not runtime.RECORD.is_file():
            return None
        record = json.loads(runtime.RECORD.read_text())
        current = {"pid": record["pid"], "command": server_control._command(record["pid"])}
        model = managed_gguf(current)
        if model:
            return {**current, "running": True, "trusted": True, "loading": True,
                    "active_model": model, "format": "GGUF"}
    except (OSError, ValueError, KeyError):
        pass
    return None


def start_gguf(alias, *, thinking=False):
    find_model(alias)
    profile = gguf_profile(alias)
    runtime = gguf_runtime()
    if runtime is None or not hasattr(runtime, "start_gguf_if_idle"):
        raise ValueError("GGUFの起動ヘルパーを更新してください")
    return runtime.start_gguf_if_idle(profile["target"], thinking=thinking)


def _model_path(reference):
    path = Path(reference).expanduser()
    if path.is_absolute():
        return path.resolve()
    local = server_control.SERVER_ROOT / path
    if reference.startswith(("./", "../")) or local.exists():
        return local.resolve()
    return None


def _model_key(reference, repos):
    path = _model_path(reference)
    for repo in repos:
        if reference == repo.repo_id or (path is not None and path.is_relative_to(repo.repo_path.resolve())):
            return ("cache", repo.repo_id)
    return ("path", str(path)) if path is not None else ("model", reference)


def _body_files(item, repos):
    if item["format"] == "GGUF":
        return {path.resolve() for path in gguf_files(item["alias"])}
    repo = next((repo for repo in repos if repo.repo_id == item["model"]), None)
    return {file.blob_path.resolve() for revision in repo.revisions for file in revision.files} if repo else set()


def _running_path(reference):
    path = _model_path(reference)
    # ps output does not establish an external process's working directory.
    if path is not None and not Path(reference).expanduser().is_absolute():
        raise ValueError("起動中モデルの相対パスを確認できないため、削除しません")
    return path


def _running_files(current, repos):
    active = current["active_model"]
    profiles = gguf_profiles()
    files = set()
    command = current.get("command", "")
    references = [active]
    for flag, reference in re.findall(
            r"(?:^|\s)(--model|--draft-model|--model-draft|--adapter-path|--mmproj|-m|-md)(?:=|\s+)(.+?)(?=\s+--?[A-Za-z]|$)", command):
        if flag == "-m" and "llama-server" not in command:
            continue  # Python's -m names the server module, not model weights.
        references.append(reference.strip("'\""))
    for reference in references:
        profile = next((profile for profile in profiles if profile["model"] == reference), None)
        if profile:
            files.update(path.resolve() for path in gguf_files(profile["alias"]))
            continue
        repo = next((repo for repo in repos if repo.repo_id == reference), None)
        if repo:
            files.update(file.blob_path.resolve() for revision in repo.revisions for file in revision.files)
            continue
        path = _running_path(reference)
        if path is None:
            if reference != active:
                raise ValueError("起動中モデルの補助ファイルを確認できないため、削除しません")
            continue
        if not path.exists():
            raise ValueError("起動中モデルのファイルを確認できないため、削除しません")
        if path.is_dir():
            files.update(child.resolve() for child in path.rglob("*") if child.is_file())
        else:
            files.add(path)
            shard = re.fullmatch(r"(.+)-[0-9]{5}-of-([0-9]{5})\.gguf", path.name)
            if shard:
                files.update(part.resolve() for part in path.parent.glob(f"{shard[1]}-?????-of-{shard[2]}.gguf"))
    return files


def _guard(item, body):
    current = server_control.status()
    listener = server_control._listener_pid(strict=True)
    if listener is not None and (not current["running"] or listener != current.get("pid")):
        raise ValueError("8080のプロセスが変わったため、削除内容を確認し直してください")
    if current["running"]:
        active = current.get("active_model", "")
        if not active:
            raise ValueError("起動中のモデルを確認できないため、削除しません")
        repos = _cache_repos()
        active_key = _model_key(active, repos)
        if _model_key(item["model"], repos) == active_key:
            raise ValueError("起動中のモデルは削除できません")
        active_path = _running_path(active)
        profile = next((profile for profile in gguf_profiles() if profile["model"] == active), None)
        if profile:
            active_files = gguf_files(profile["alias"])
            active_path = active_files[0].resolve() if active_files else active_path
        if item["format"] == "GGUF":
            files = gguf_files(item["alias"])
            if files and active_path == files[0].resolve():
                raise ValueError("起動中のモデルは削除できません")
        known = (profile is not None or active_key[0] == "cache"
                 or (active_path is not None and active_path.exists())
                 or any(_model_key(model["model"], repos) == active_key for model in models()))
        if not known:
            raise ValueError("起動中のモデルを確認できないため、削除しません")
        if body and _body_files(item, repos) & _running_files(current, repos):
            raise ValueError("起動中のモデルと本体を共有しているため、削除できません")
    if body:
        ensure_lab_idle()


def deletion_preview(alias, body=False):
    item = find_model(alias)
    _guard(item, body)
    files, revisions, location = [], [], ""
    if body:
        if item["format"] == "GGUF":
            files = [_file_info(path) for path in gguf_files(alias)]
            location = str(GGUF_ROOT / "models")
        else:
            repo = next((repo for repo in _cache_repos() if repo.repo_id == item["model"]), None)
            if repo is None:
                raise ValueError("本体削除に対応する共有キャッシュがありません。登録だけ外せます")
            revisions = sorted(revision.commit_hash for revision in repo.revisions)
            files = sorted({_file_info(file.blob_path)[0]: _file_info(file.blob_path)
                            for revision in repo.revisions for file in revision.files}.values())
            location = str(repo.repo_path)
    snapshot = {"alias": alias, "model": item["model"], "body": body,
                "files": files, "revisions": revisions,
                "config": server_control.MODELS_CONFIG.read_text(), "settings": settings()}
    fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    content = {key: snapshot[key] for key in ("model", "body", "files", "revisions")}
    content_fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
    return {"alias": alias, "name": item["name"], "body": body,
            "bytes": sum(file[1] for file in files), "location": location,
            "file_count": len(files), "fingerprint": fingerprint, "content_fingerprint": content_fingerprint}


def delete_model(alias, body, fingerprint):
    preview = deletion_preview(alias, body)
    if not fingerprint or preview["fingerprint"] != fingerprint:
        raise ValueError("確認後にモデルや設定が変わりました。削除内容を確認し直してください")
    _delete_item(find_model(alias), body)
    return preview


def _delete_item(item, body):
    alias = item["alias"]
    removed_aliases = {alias}
    if body and item["format"] == "MLX":
        removed_aliases.update(entry["alias"] for entry in server_control.aliases() if entry["model"] == item["model"])
    if body:
        if item["format"] == "GGUF":
            for path in gguf_files(alias):
                path.unlink()
        else:
            delete_cached_model(item["model"])
    if item["format"] == "GGUF":
        def hide(value):
            value["hidden_gguf_aliases"] = sorted(set(value.get("hidden_gguf_aliases", [])) | {alias})
        update_settings(hide)
    else:
        original = server_control.MODELS_CONFIG.read_text()
        # Remove every alias for this model when deleting its shared weights.
        lines = [line for line in original.splitlines(keepends=True)
                 if not (line.strip() and not line.lstrip().startswith("#") and "=" in line
                         and (line.split("=", 1)[0].strip() == alias
                              or (body and line.split("=", 1)[1].strip() == item["model"])))]
        temporary = server_control.MODELS_CONFIG.with_suffix(".tmp")
        temporary.write_text("".join(lines))
        temporary.replace(server_control.MODELS_CONFIG)
    if settings().get("default_alias") in removed_aliases:
        def clear_deleted_default(value):
            if value.get("default_alias") in removed_aliases:
                value.pop("default_alias", None)
        update_settings(clear_deleted_default)


def deletion_preview_many(aliases, body=False):
    if (not isinstance(aliases, list) or not 1 <= len(aliases) <= 100
            or any(not isinstance(alias, str) or not alias for alias in aliases)
            or len(set(aliases)) != len(aliases)):
        raise ValueError("対象モデルを選択してください")
    previews, seen = [], set()
    for alias in aliases:
        item = find_model(alias)
        key = item["model"] if body else alias
        if key in seen:
            continue
        seen.add(key)
        previews.append(deletion_preview(alias, body))
    snapshot = {"aliases": aliases, "body": body, "items": previews}
    fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return {**snapshot, "fingerprint": fingerprint,
            "bytes": sum(item["bytes"] for item in previews)}


def delete_models(aliases, body, fingerprint):
    # Validate the complete selection before any destructive operation.
    preview = deletion_preview_many(aliases, body)
    if not fingerprint or preview["fingerprint"] != fingerprint:
        raise ValueError("確認後にモデルや設定が変わりました。削除内容を確認し直してください")
    completed = []
    for expected in preview["items"]:
        try:
            fresh = deletion_preview(expected["alias"], body)
            # Earlier deletions intentionally change registration/settings; weights must stay identical.
            if fresh["content_fingerprint"] != expected["content_fingerprint"]:
                raise ValueError("確認後にモデル本体が変わりました")
            _delete_item(find_model(expected["alias"]), body)
            completed.append(expected["name"])
        except Exception as error:
            done = "、".join(completed) or "なし"
            raise ValueError(f"削除を中断しました。完了: {done}。未完了: {expected['name']} 以降。{error}") from error
    return preview


def ensure_lab_idle():
    """A detached experiment can outlive the Lab UI; check full process commands."""
    result = subprocess.run(["/bin/ps", "-A", "-ww", "-o", "command="],
                            capture_output=True, text=True, timeout=5)
    if result.returncode or not result.stdout.strip():
        raise ValueError("実験ラボの使用状況を確認できないため、本体を削除しません")
    pattern = r"(?:^|\s)-m\s+mlx_activation_lab(?:\.webapp(?:\s|$)|\s+(?:app|run|capture|inspect|steer|wizard)(?:\s|$))"
    console_pattern = r"(?:^|[\s/])mlx-activation-lab\s+(?:app|run|capture|inspect|steer|wizard)(?:\s|$)"
    if re.search(pattern, result.stdout, re.M) or re.search(console_pattern, result.stdout, re.M):
        raise ValueError("実験ラボと実行中の実験を終了してから、本体を削除してください")


def remember_start(pid, alias, *, thinking=False):
    started = process_started(pid)
    if not started:
        raise RuntimeError("起動プロセスの記録を確認できません")
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    PENDING.write_text(json.dumps({"pid": pid, "started": started, "alias": alias, "thinking": thinking}) + "\n")


def process_started(pid):
    return subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                          capture_output=True, text=True, timeout=5).stdout.strip()


def pending_mlx():
    if not PENDING.is_file():
        return None
    try:
        record = json.loads(PENDING.read_text())
        if record.get("started") and process_started(record["pid"]) == record["started"]:
            model = next((item["model"] for item in server_control.aliases() if item["alias"] == record["alias"]), "")
            command = server_control._command(record["pid"])
            return {"running": True, "loading": True, "pid": record["pid"], "command": command,
                    "trusted": server_control._trusted(command), "active_model": model, "format": "MLX",
                    "thinking": record.get("thinking", server_control._thinking_from_command(command))}
    except (OSError, ValueError, KeyError):
        pass
    return None
