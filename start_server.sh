#!/bin/bash
# ==============================================
# Qwen3.6-35B-A3B-4bit-DWQ MLX Server 起動スクリプト
# エージェント用（ツール実行対応）
# ==============================================

# venv 有効化
source ~/mlx-server/.venv/bin/activate

MODEL_PATH="$HOME/.cache/huggingface/hub/models--mlx-community--Qwen3.6-35B-A3B-4bit-DWQ/snapshots/73c707af4243243b18193444467872d20cff9399"
HOST="127.0.0.1"
PORT="8080"

# ---- 安全・パフォーマンス設定 ----
MAX_TOKENS=8192
CONTEXT_LENGTH=65536     # Hermesのcontext_length設定に合わせた

echo "🚀 Starting Qwen3.6-35B-A3B-4bit-DWQ MLX Server on ${HOST}:${PORT}"
echo "   Model: ${MODEL_PATH}"
echo "   Context: ${CONTEXT_LENGTH} tokens"

mlx_lm.server \
  --model "${MODEL_PATH}" \
  --host "${HOST}" \
  --port "${PORT}" \
  --max-tokens "${MAX_TOKENS}" \
  --trust-remote-code \
  --chat-template-args '{"enable_thinking": false}'