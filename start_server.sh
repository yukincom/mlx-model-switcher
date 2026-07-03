#!/bin/bash
# ==============================================
# MLX Server 起動スクリプト（モデル切り替え対応版）
# 使い方:
#   ./start_server.sh <エイリアス名>   → 指定モデルで起動
#   ./start_server.sh list             → 登録モデル一覧を表示
#   ./start_server.sh                  → 一覧を表示して終了
# ==============================================

set -e

MLX_SERVER_DIR="$HOME/mlx-server"
CONFIG_FILE="${MLX_SERVER_DIR}/models.conf"
HOST="127.0.0.1"
PORT="8080"

# ---- 安全・パフォーマンス設定（共通） ----
MAX_TOKENS=8192
CONTEXT_LENGTH=65536     # Hermesのcontext_length設定に合わせた

# ==============================================
# 矢印キー選択メニュー（外部ツール不要・bash標準機能のみ）
# 使い方: selected=$(select_menu "選択肢1" "選択肢2" "選択肢3")
# ==============================================
select_menu() {
  local options=("$@")
  local selected=0
  local key
  local num_options=${#options[@]}

  tput civis >&2   # カーソル非表示

  while true; do
    for i in "${!options[@]}"; do
      if [ "$i" -eq "$selected" ]; then
        echo -e "\033[7m> ${options[$i]}\033[0m" >&2   # ハイライト（反転表示）
      else
        echo "  ${options[$i]}" >&2
      fi
    done

    IFS= read -rsn1 key
    if [[ "$key" == $'\x1b' ]]; then
      read -rsn2 key
      case "$key" in
        '[A') ((selected--)); [ "$selected" -lt 0 ] && selected=$((num_options - 1)) ;;  # ↑
        '[B') ((selected++)); [ "$selected" -ge "$num_options" ] && selected=0 ;;         # ↓
      esac
    elif [[ -z "$key" ]]; then
      break   # Enter
    fi

    tput cuu "$num_options" >&2   # 描画位置をメニュー先頭に戻す
  done

  tput cnorm >&2   # カーソル再表示
  echo "${options[$selected]}"
}


# venv 有効化
source "${MLX_SERVER_DIR}/.venv/bin/activate"

# ---- 設定ファイルチェック ----
if [ ! -f "$CONFIG_FILE" ]; then
  echo "❌ 設定ファイルが見つかりません: ${CONFIG_FILE}"
  echo "   先に models.conf を作成してください"
  exit 1
fi

echo "🚀 起動するモデルを選択（↑↓ + Enter）" >&2
  ALIASES=($(grep -v '^#' "$CONFIG_FILE" | grep -v '^$' | cut -d'=' -f1))
  ALIAS=$(select_menu "${ALIASES[@]}")

# ---- モデルパス解決 ----
MODEL_PATH=$(grep "^${ALIAS}=" "$CONFIG_FILE" | cut -d'=' -f2-)

if [ -z "$MODEL_PATH" ]; then
  echo "❌ エイリアス '${ALIAS}' が models.conf に見つかりません"
  echo "   ./start_server.sh list で一覧を確認してください"
  exit 1
fi

# ---- パス内の ~ を展開 ----
MODEL_PATH="${MODEL_PATH/#\~/$HOME}"

# ---- snapshots/latest 指定の場合、実ハッシュを自動解決 ----
# （ハッシュ変更のたびにconfファイルを書き換えなくて済むように）
if [[ "$MODEL_PATH" == *"/snapshots/latest" ]]; then
  BASE_DIR="${MODEL_PATH%/snapshots/latest}"
  RESOLVED=$(ls -td "${BASE_DIR}"/snapshots/*/ 2>/dev/null | head -n 1)
  if [ -z "$RESOLVED" ]; then
    echo "❌ スナップショットが見つかりません: ${BASE_DIR}/snapshots/"
    exit 1
  fi
  MODEL_PATH="${RESOLVED%/}"
fi

if [ ! -d "$MODEL_PATH" ]; then
  echo "❌ モデルディレクトリが存在しません: ${MODEL_PATH}"
  exit 1
fi

echo "🚀 Starting MLX Server on ${HOST}:${PORT}"
echo "   Alias:   ${ALIAS}"
echo "   Model:   ${MODEL_PATH}"
echo "   Context: ${CONTEXT_LENGTH} tokens"

mlx_lm.server \
  --model "${MODEL_PATH}" \
  --host "${HOST}" \
  --port "${PORT}" \
  --max-tokens "${MAX_TOKENS}" \
  --trust-remote-code \
  --chat-template-args '{"enable_thinking": false}'