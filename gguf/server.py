"""8080の既知モデルを1つずつ切り替える。MLXの停止・起動は既存管理APIを使う。"""
from pathlib import Path
import argparse
import fcntl
import json
import os
import signal
import socket
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
MODELS_ROOT = Path.home() / ".cache/gguf/models"
BINARY = ROOT / "runtime/bin/llama-server"
RECORD = ROOT / "evidence/llama-server.json"
MANAGED_RECORD = ROOT / "evidence/managed-server.json"
ALIASES = {"27b": "Qwen3.8-27B-8bit", "gemma": "gemma-4-26B-A4B-it-QAT-MLX-4bit"}
PORT = 8080
GGUF_PROFILES = {
    "125b": {"alias": "gguf:Qwen3.8-Flash-Next-125B",
             "model": "Qwen3.8-Flash-Next-125B-IQ3_XXS", "name": "Qwen3.8-Flash-Next-125B",
             "manifest": "models-manifest.json", "verified": "evidence/download-verified.json", "context": 16384},
    "gemma-gguf": {"alias": "gguf:Gemma-4-26B-A4B-QAT-UD-Q4_K_XL",
                   "model": "Gemma-4-26B-A4B-QAT-UD-Q4_K_XL", "name": "Gemma-4-26B-QAT-UD-Q4_K_XL",
                   "manifest": "gemma4-qat-manifest.json",
                   "mmproj": "mmproj-F16.gguf",
                   "draft": "mtp-gemma-4-26B-A4B-it.gguf",
                   "draft_verified": "evidence/gemma4-qat-2026-10-17/mtp-verified.json",
                   "context": 65536,
                   "vision_verified": "evidence/gemma4-qat-2026-10-17/mmproj-verified.json",
                   "verified": "evidence/gemma4-qat-2026-10-17/download-verified.json"},
}


def gguf_profiles():
    """管理画面へモデル別の登録情報を返す。重みの取得・起動は行わない。"""
    return [{"target": target, **profile} for target, profile in GGUF_PROFILES.items()]


def api(path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Server-Token"] = token
    request = urllib.request.Request("http://127.0.0.1:8766" + path,
        None if body is None else json.dumps(body).encode(), headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def process_identity(pid):
    result = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", "lstart=,command="],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def is_our_llama(current):
    if not RECORD.is_file():
        return False
    record = json.loads(RECORD.read_text())
    return (current["pid"] == record["pid"]
            and current["command"].startswith(str(BINARY) + " ")
            and process_identity(record["pid"]) == record["identity"])


def wait_idle(timeout=60):
    """進行中の接続が完了して3秒間空くまで待ち、別の要求を途中で切らない。"""
    deadline, idle_since = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        result = subprocess.run(["/usr/sbin/lsof", "-nP", "-iTCP:8080", "-sTCP:ESTABLISHED", "-t"],
                                capture_output=True, text=True, check=False)
        if result.returncode not in (0, 1) or result.stderr.strip():
            raise RuntimeError("8080への接続状態を確認できません")
        if result.stdout.strip():
            idle_since = None
        else:
            idle_since = idle_since or time.monotonic()
            if time.monotonic() - idle_since >= 3:
                return
        time.sleep(0.5)
    raise RuntimeError("8080で推論が続いているため、切替は実行していません")


def stop():
    state = api("/api/status")
    current = state["server"]
    if not current["running"]:
        if MANAGED_RECORD.is_file():
            pending = json.loads(MANAGED_RECORD.read_text())
            if pending["identity"] and process_identity(pending["pid"]) == pending["identity"]:
                os.kill(pending["pid"], signal.SIGTERM)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process_identity(pending["pid"]) != pending["identity"]:
                        return
                    time.sleep(0.25)
                raise RuntimeError("起動中の前モデルが終了していません")
        return
    if not current["trusted"] and not is_our_llama(current):
        raise RuntimeError("8080のプロセスが管理対象と一致しません")
    identity = process_identity(current["pid"])
    wait_idle()
    latest = api("/api/status")
    if latest["server"] != current or process_identity(current["pid"]) != identity:
        raise RuntimeError("待機中に8080のプロセスが変わりました")
    if current["trusted"]:
        api("/api/stop", {"pid": current["pid"], "command": current["command"]}, latest["token"])
    else:
        os.kill(current["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process_identity(current["pid"]) != identity:
            return
        time.sleep(0.25)
    raise RuntimeError("前のモデルが終了していません。次のモデルは起動していません")


def validated_125b_files():
    """Validate local inputs without stopping a server or making API calls."""
    return validated_gguf_files("125b")


def validated_gguf_files(target):
    """選択モデルの検証記録と重みを確認する。既存モデルの停止前に呼ぶ。"""
    profile = GGUF_PROFILES[target]
    if not BINARY.is_file() or not (ROOT / profile["verified"]).is_file():
        raise RuntimeError(f"{profile['name']}のビルド・重み検証がまだ完了していません")
    if profile.get("vision_verified") and not (ROOT / profile["vision_verified"]).is_file():
        raise RuntimeError(f"{profile['name']}の画像用ファイルの検証がまだ完了していません")
    if profile.get("draft_verified") and not (ROOT / profile["draft_verified"]).is_file():
        raise RuntimeError(f"{profile['name']}の先読みモデルの検証がまだ完了していません")
    items = json.loads((ROOT / profile["manifest"]).read_text())["files"]
    if profile.get("mmproj") and not any(item["file"] == profile["mmproj"] for item in items):
        raise RuntimeError("画像用ファイルの登録を確認できません")
    if profile.get("draft") and not any(item["file"] == profile["draft"] for item in items):
        raise RuntimeError("先読みモデルの登録を確認できません")
    for item in items:
        path = MODELS_ROOT / item["directory"] / item["file"]
        if not path.is_file() or path.stat().st_size != item["bytes"]:
            raise RuntimeError(f"モデルファイルを確認できません: {path.name}")
    return items


def start_125b_if_idle(*, thinking=False):
    """Manager entry point: never stop another model and never call its API."""
    return start_gguf_if_idle("125b", thinking=thinking)


def start_gguf_if_idle(target, *, thinking=False):
    """既存の切替ロックとポート確認を通して、選択したGGUFだけを起動する。"""
    if type(thinking) is not bool:
        raise ValueError("thinkingには真偽値を指定してください")
    if target not in GGUF_PROFILES:
        raise ValueError("未登録のGGUFモデルです")
    (ROOT / "evidence").mkdir(exist_ok=True)
    with (ROOT / "evidence/switch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("別のモデル切替が進行中です") from None
        items = validated_125b_files() if target == "125b" else validated_gguf_files(target)
        if MANAGED_RECORD.is_file():
            record = json.loads(MANAGED_RECORD.read_text())
            if record.get("identity") and process_identity(record["pid"]) == record["identity"]:
                raise RuntimeError("前のモデルが起動中または終了中です")
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", PORT))
        return (_launch_125b(items, thinking=thinking) if target == "125b"
                else _launch_gguf(target, items, thinking=thinking))


def start(target):
    # 停止する前に、目的のモデルが準備済みかを確認する。
    if target in GGUF_PROFILES:
        items = validated_gguf_files(target)
    else:
        if ALIASES[target] not in {item["alias"] for item in api("/api/status")["aliases"]}:
            raise RuntimeError("MLXモデルが未登録です")
    stop()
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 8080))
    if target not in GGUF_PROFILES:
        state = api("/api/status")
        result = api("/api/start", {"alias": ALIASES[target]}, state["token"])
        pid = result["result"]["pid"]
        MANAGED_RECORD.write_text(json.dumps({"pid": pid, "identity": process_identity(pid), "target": target}) + "\n")
        print(json.dumps({"event": "loading", "target": target, **result}), flush=True)
        return
    return _launch_gguf(target, items)


def _launch_125b(items, *, thinking=False):
    return _launch_gguf("125b", items, thinking=thinking)


def _launch_gguf(target, items, *, thinking=False):
    """検証済み重みから起動し、PIDと起動時刻・モデルを同じ記録へ保存する。"""
    if type(thinking) is not bool:
        raise ValueError("thinkingには真偽値を指定してください")
    profile = GGUF_PROFILES[target]
    args = [str(BINARY), "-m", str(MODELS_ROOT / items[0]["directory"] / items[0]["file"])]
    env = dict(os.environ, GGML_ALLOC_FREE_DELAY="4", LLAMA_GRAPH_CACHE="16")
    if target == "125b":
        args += ["-md", str(MODELS_ROOT / items[2]["directory"] / items[2]["file"]),
                 "--spec-type", "draft-mtp", "--spec-draft-n-max", "7"]
        env.update(LLAMA_SPEC_MTP_MERGE="1", LLAMA_SPEC_STOP="1")
    else:
        projector = next(item for item in items if item["file"] == profile["mmproj"])
        draft = next(item for item in items if item["file"] == profile["draft"])
        args += ["--mmproj", str(MODELS_ROOT / projector["directory"] / projector["file"])]
        # 同じGemma QAT用の先読みを使う。Qwen専用の停止則・合流設定は引き継がない。
        args += ["-md", str(MODELS_ROOT / draft["directory"] / draft["file"]),
                 "--spec-type", "draft-mtp", "--spec-draft-n-max", "2", "-ngld", "99"]
        # Gemmaの画像は非因果attention。1120画像tokenを小さいubatchで切り詰めない。
        args += ["-b", "2048", "-ub", "2048"]
        for name in ("LLAMA_SPEC_MTP_MERGE", "LLAMA_SPEC_STOP", "LLAMA_SPEC_STOP_PARAMS"):
            env.pop(name, None)
    args += ["--reasoning", "on" if thinking else "off", "-ngl", "99", "-c", str(profile["context"]), "-np", "1",
            "--host", "127.0.0.1", "--port", str(PORT),
            "--alias", "default_model"]
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "evidence").mkdir(exist_ok=True)
    with (ROOT / "logs/llama-server.log").open("ab") as log:
        process = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True)
    time.sleep(0.5)
    if process.poll() is not None:
        raise RuntimeError(f"{profile['name']}サーバーが終了しました。logs/llama-server.logを確認してください")
    record = {"pid": process.pid, "identity": process_identity(process.pid), "argv": args,
              "started_local": time.strftime("%Y-%m-%d %H:%M:%S %z"),
              "target": target, "alias": profile["alias"], "model": profile["model"], "thinking": thinking,
              "source_revision": json.loads((ROOT / "models-manifest.json").read_text())["source_revision"]}
    RECORD.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    MANAGED_RECORD.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"event": "loading", "target": target, "pid": process.pid}), flush=True)
    return process.pid


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "switch", "stop"])
    parser.add_argument("target", nargs="?", choices=[*GGUF_PROFILES, *ALIASES])
    args = parser.parse_args()
    if args.action == "status":
        state = api("/api/status")["server"]
        state["managed_gguf"] = is_our_llama(state)
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        if args.action == "switch" and not args.target:
            parser.error("switchには125b / 27b / gemma / gemma-ggufを指定してください")
        (ROOT / "evidence").mkdir(exist_ok=True)
        with (ROOT / "evidence/switch.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit("別のモデル切替が進行中です")
            if args.action == "stop":
                stop()
                print("8080のモデルを停止しました")
            else:
                start(args.target)
