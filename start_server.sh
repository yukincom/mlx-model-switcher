#!/bin/bash
# ==============================================
# MLX Server 起動スクリプト（モデル切り替え対応版）
# 使い方:
#   ./start_server.sh <エイリアス名>   → 指定モデルで起動
#   ./start_server.sh list             → 登録モデル一覧を表示
#   ./start_server.sh                  → 対話メニューから選んで起動
# ==============================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MLX_SERVER_DIR="${MLX_SERVER_DIR:-${SCRIPT_DIR}}"
CONFIG_FILE="${MLX_MODELS_CONFIG:-${MLX_SERVER_DIR}/models.conf}"
HOST="${MLX_HOST:-127.0.0.1}"
PORT="${MLX_PORT:-8080}"

# ---- 安全・パフォーマンス設定（環境変数で一時上書き可能） ----
DEFAULT_MAX_TOKENS="${MLX_MAX_TOKENS:-8192}"
CLIENT_CONTEXT_TARGET="${MLX_CLIENT_CONTEXT_TARGET:-65536}"
DECODE_CONCURRENCY="${MLX_DECODE_CONCURRENCY:-2}"
PROMPT_CONCURRENCY="${MLX_PROMPT_CONCURRENCY:-1}"
PREFILL_STEP_SIZE="${MLX_PREFILL_STEP_SIZE:-512}"
PROMPT_CACHE_BYTES="${MLX_PROMPT_CACHE_BYTES:-4GB}"
ALLOWED_ORIGINS="${MLX_ALLOWED_ORIGINS:-http://127.0.0.1:${PORT},http://localhost:${PORT}}"

CURSOR_HIDDEN=0
PARSED_ALIAS=""
PARSED_MODEL_REF=""
SELECTED_ALIAS=""

restore_cursor() {
  if [ "${CURSOR_HIDDEN:-0}" -eq 1 ]; then
    tput cnorm >&2 2>/dev/null || true
    CURSOR_HIDDEN=0
  fi
}

trap restore_cursor EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

usage() {
  cat <<'EOF'
使い方:
  ./start_server.sh                  対話メニューから選んで起動
  ./start_server.sh <エイリアス名>  指定モデルで起動
  ./start_server.sh list             登録モデル一覧を表示
  ./start_server.sh --help           この説明を表示
EOF
}

trim_whitespace() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

parse_model_line() {
  local line
  line="$(trim_whitespace "$1")"

  case "$line" in
    ""|\#*) return 1 ;;
  esac

  if [[ "$line" != *=* ]]; then
    return 1
  fi

  PARSED_ALIAS="$(trim_whitespace "${line%%=*}")"
  PARSED_MODEL_REF="$(trim_whitespace "${line#*=}")"
  [ -n "$PARSED_ALIAS" ] && [ -n "$PARSED_MODEL_REF" ]
}

list_models() {
  local line
  echo "登録モデル:"
  while IFS= read -r line || [ -n "$line" ]; do
    if parse_model_line "$line"; then
      printf '  %-48s %s\n' "$PARSED_ALIAS" "$PARSED_MODEL_REF"
    fi
  done < "$CONFIG_FILE"
}

load_aliases() {
  local line
  ALIASES=()
  while IFS= read -r line || [ -n "$line" ]; do
    if parse_model_line "$line"; then
      ALIASES[${#ALIASES[@]}]="$PARSED_ALIAS"
    fi
  done < "$CONFIG_FILE"

  if [ "${#ALIASES[@]}" -eq 0 ]; then
    echo "❌ 登録モデルがありません: ${CONFIG_FILE}" >&2
    exit 1
  fi
}

find_model_ref() {
  local target="$1"
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    if parse_model_line "$line" && [ "$PARSED_ALIAS" = "$target" ]; then
      printf '%s' "$PARSED_MODEL_REF"
      return 0
    fi
  done < "$CONFIG_FILE"
  return 1
}

# ==============================================
# 矢印キー選択メニュー（外部ツール不要・bash標準機能のみ）
# ==============================================
select_menu() {
  local options=("$@")
  local selected=0
  local key=""
  local num_options=${#options[@]}
  local i

  if command -v tput >/dev/null 2>&1 && tput civis >&2 2>/dev/null; then
    CURSOR_HIDDEN=1
  fi

  while true; do
    for i in "${!options[@]}"; do
      if [ "$i" -eq "$selected" ]; then
        printf '\033[7m> %s\033[0m\n' "${options[$i]}" >&2
      else
        printf '  %s\n' "${options[$i]}" >&2
      fi
    done

    if ! IFS= read -rsn1 key; then
      echo >&2
      return 1
    fi

    if [[ "$key" == $'\x1b' ]]; then
      IFS= read -rsn2 key || true
      case "$key" in
        '[A')
          selected=$((selected - 1))
          if [ "$selected" -lt 0 ]; then
            selected=$((num_options - 1))
          fi
          ;;
        '[B')
          selected=$((selected + 1))
          if [ "$selected" -ge "$num_options" ]; then
            selected=0
          fi
          ;;
      esac
    elif [[ -z "$key" ]]; then
      break
    fi

    if ! tput cuu "$num_options" >&2 2>/dev/null; then
      printf '\033[%sA' "$num_options" >&2
    fi
  done

  SELECTED_ALIAS="${options[$selected]}"
  restore_cursor
}

require_positive_integer() {
  local name="$1"
  local value="$2"
  if [[ -z "$value" || "$value" == *[!0-9]* || "$value" -eq 0 ]]; then
    echo "❌ ${name} は1以上の整数で指定してください: ${value}" >&2
    exit 1
  fi
}

hf_hub_cache_dir() {
  if [ -n "${HF_HUB_CACHE:-}" ]; then
    printf '%s' "$HF_HUB_CACHE"
  elif [ -n "${HF_HOME:-}" ]; then
    printf '%s/hub' "$HF_HOME"
  elif [ -n "${XDG_CACHE_HOME:-}" ]; then
    printf '%s/huggingface/hub' "$XDG_CACHE_HOME"
  else
    printf '%s/.cache/huggingface/hub' "$HOME"
  fi
}

resolve_snapshot_dir() {
  local model_root="$1"
  local label="$2"
  local revision=""
  local candidate=""
  local snapshot
  local count=0

  if [ -f "${model_root}/refs/main" ]; then
    IFS= read -r revision < "${model_root}/refs/main" || true
    if [[ "$revision" =~ ^[0-9a-fA-F]{7,64}$ ]] && [ -d "${model_root}/snapshots/${revision}" ]; then
      printf '%s' "${model_root}/snapshots/${revision}"
      return 0
    fi
    echo "❌ refs/main が有効なスナップショットを指していません: ${label}" >&2
    return 1
  fi

  for snapshot in "${model_root}"/snapshots/*; do
    [ -d "$snapshot" ] || continue
    candidate="$snapshot"
    count=$((count + 1))
  done

  if [ "$count" -eq 1 ]; then
    printf '%s' "$candidate"
    return 0
  fi

  if [ "$count" -eq 0 ]; then
    echo "❌ ローカルキャッシュにスナップショットがありません: ${label}" >&2
  else
    echo "❌ refs/main がなく、スナップショットを一意に選べません: ${label}" >&2
  fi
  return 1
}

if [ "$#" -eq 1 ] && { [ "$1" = "--help" ] || [ "$1" = "-h" ] || [ "$1" = "help" ]; }; then
  usage
  exit 0
fi

if [ "$#" -gt 1 ]; then
  usage >&2
  exit 2
fi

if [ ! -f "$CONFIG_FILE" ]; then
  echo "❌ 設定ファイルが見つかりません: ${CONFIG_FILE}" >&2
  echo "   先に models.conf を作成してください" >&2
  exit 1
fi

if [ "$#" -eq 1 ] && [ "$1" = "list" ]; then
  list_models
  exit 0
fi

if [ "$#" -eq 1 ]; then
  ALIAS="$1"
else
  if [ ! -t 0 ]; then
    echo "❌ 対話メニューにはターミナルが必要です。エイリアス名を指定してください。" >&2
    usage >&2
    exit 2
  fi
  load_aliases
  echo "🚀 起動するモデルを選択（↑↓ + Enter）" >&2
  select_menu "${ALIASES[@]}"
  ALIAS="$SELECTED_ALIAS"
fi

if ! MODEL_REF="$(find_model_ref "$ALIAS")"; then
  echo "❌ エイリアス '${ALIAS}' が models.conf に見つかりません" >&2
  echo "   ./start_server.sh list で一覧を確認してください" >&2
  exit 1
fi

MODEL_ARGUMENT=""
MODEL_LOCAL_PATH=""
MODEL_REF_IS_LOCAL=0

case "$MODEL_REF" in
  /*|\~|\~/*|./*|../*) MODEL_REF_IS_LOCAL=1 ;;
esac

if [ "$MODEL_REF_IS_LOCAL" -eq 0 ] && [ -d "${MLX_SERVER_DIR}/${MODEL_REF}" ]; then
  MODEL_REF_IS_LOCAL=1
fi

# Hugging Face IDは、ダウンロード済みであることを確認してからIDのまま渡す。
# Hermesのrequest.modelと一致するため、初回リクエストでの不要な再ロードを防げる。
if [ "$MODEL_REF_IS_LOCAL" -eq 0 ] && [[ "$MODEL_REF" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]]; then
  HUB_CACHE="$(hf_hub_cache_dir)"
  MODEL_CACHE_ROOT="${HUB_CACHE}/models--${MODEL_REF//\//--}"
  if ! MODEL_LOCAL_PATH="$(resolve_snapshot_dir "$MODEL_CACHE_ROOT" "$MODEL_REF")"; then
    exit 1
  fi
  MODEL_ARGUMENT="$MODEL_REF"
else
  MODEL_PATH="${MODEL_REF/#\~/$HOME}"
  if [[ "$MODEL_PATH" != /* ]]; then
    MODEL_PATH="${MLX_SERVER_DIR}/${MODEL_PATH#./}"
  fi

  if [[ "$MODEL_PATH" == */snapshots/latest ]]; then
    MODEL_ROOT="${MODEL_PATH%/snapshots/latest}"
    if ! MODEL_LOCAL_PATH="$(resolve_snapshot_dir "$MODEL_ROOT" "$MODEL_REF")"; then
      exit 1
    fi
  else
    MODEL_LOCAL_PATH="$MODEL_PATH"
  fi
  MODEL_ARGUMENT="$MODEL_LOCAL_PATH"
fi

if [ ! -d "$MODEL_LOCAL_PATH" ] || [ ! -f "$MODEL_LOCAL_PATH/config.json" ]; then
  echo "❌ モデル設定ファイルを確認できません: ${MODEL_LOCAL_PATH}" >&2
  exit 1
fi

MODEL_HAS_WEIGHTS=0
for MODEL_WEIGHT in "${MODEL_LOCAL_PATH}"/*.safetensors; do
  if [ -s "$MODEL_WEIGHT" ]; then
    MODEL_HAS_WEIGHTS=1
    break
  fi
done

if [ "$MODEL_HAS_WEIGHTS" -eq 0 ]; then
  echo "❌ モデル重み（*.safetensors）を確認できません: ${MODEL_LOCAL_PATH}" >&2
  exit 1
fi

require_positive_integer "MLX_PORT" "$PORT"
require_positive_integer "MLX_MAX_TOKENS" "$DEFAULT_MAX_TOKENS"
require_positive_integer "MLX_CLIENT_CONTEXT_TARGET" "$CLIENT_CONTEXT_TARGET"
require_positive_integer "MLX_DECODE_CONCURRENCY" "$DECODE_CONCURRENCY"
require_positive_integer "MLX_PROMPT_CONCURRENCY" "$PROMPT_CONCURRENCY"
require_positive_integer "MLX_PREFILL_STEP_SIZE" "$PREFILL_STEP_SIZE"

case "$HOST" in
  127.0.0.1|localhost|::1) ;;
  *)
    if [ "${MLX_ALLOW_REMOTE:-0}" != "1" ]; then
      echo "❌ localhost以外での公開には MLX_ALLOW_REMOTE=1 の明示指定が必要です: ${HOST}" >&2
      exit 1
    fi
    ;;
esac

if [ ! -f "${MLX_SERVER_DIR}/.venv/bin/activate" ]; then
  echo "❌ Python環境が見つかりません: ${MLX_SERVER_DIR}/.venv" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "${MLX_SERVER_DIR}/.venv/bin/activate"

if ! command -v mlx_lm.server >/dev/null 2>&1; then
  echo "❌ mlx_lm.server がPython環境に見つかりません" >&2
  exit 1
fi

echo "🚀 Starting MLX Server on ${HOST}:${PORT}"
echo "   Alias:                 ${ALIAS}"
echo "   Model:                 ${MODEL_ARGUMENT}"
echo "   Local cache:           ${MODEL_LOCAL_PATH}"
echo "   Client context target: ${CLIENT_CONTEXT_TARGET} tokens"
echo "   Default max output:    ${DEFAULT_MAX_TOKENS} tokens (request may override)"
echo "   Concurrency:           decode=${DECODE_CONCURRENCY}, prompt=${PROMPT_CONCURRENCY}"
echo "   Prefill step:          ${PREFILL_STEP_SIZE}"
echo "   Prompt cache limit:    ${PROMPT_CACHE_BYTES}"
echo "   Allowed origins:       ${ALLOWED_ORIGINS}"
echo "   Thinking:              disabled"
echo "   Network model access:  disabled (local cache only)"

# サーバーだけをオフライン化する。Yunoやドギドの環境には波及しない。
exec env \
  HF_HUB_OFFLINE=1 \
  TRANSFORMERS_OFFLINE=1 \
  mlx_lm.server \
    --model "${MODEL_ARGUMENT}" \
    --host "${HOST}" \
    --port "${PORT}" \
    --allowed-origins "${ALLOWED_ORIGINS}" \
    --max-tokens "${DEFAULT_MAX_TOKENS}" \
    --decode-concurrency "${DECODE_CONCURRENCY}" \
    --prompt-concurrency "${PROMPT_CONCURRENCY}" \
    --prefill-step-size "${PREFILL_STEP_SIZE}" \
    --prompt-cache-bytes "${PROMPT_CACHE_BYTES}" \
    --chat-template-args '{"enable_thinking": false}'
