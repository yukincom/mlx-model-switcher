# MLX Server 起動スクリプト（モデル切り替え対応版）

[![CI](https://github.com/yukincom/mlx-model-switcher/actions/workflows/ci.yml/badge.svg)](https://github.com/yukincom/mlx-model-switcher/actions/workflows/ci.yml)

Apple Silicon（MLX）向け**ローカルLLMサーバー**を快適に運用するためのBash起動スクリプトです。

矢印キーだけでモデルを選択できる対話型メニューと、`models.conf`によるエイリアス管理で、複数のモデルを切り替えられます。
OpenAI互換APIとして、**Hermes Agent**など、外部LLMの接続先を設定できるアプリから利用できます。

![sample.png](https://github.com/yukincom/mlx-model-switcher/blob/main/sample.png)

---

## ✨ 主な特徴

- **矢印キー選択メニュー**（↑↓ + Enterのみ）
- `models.conf` でエイリアス管理
- エイリアスを引数にした非対話起動と一覧表示
- Hugging Face IDと`refs/main`による確実なローカルキャッシュ解決
- 長文向けの小分け読み込み、キャッシュ上限、控えめな同時実行数
- localhostを標準にし、CORSを制限。Hugging Faceからの自動取得と`--trust-remote-code`を無効化
- Hermes Agent対応（ダウンロード済みモデルのみ切り替え可能）
- 設定ファイルにコメントで整理しやすい

---

## 📁 インストール

```bash
git clone https://github.com/yukincom/mlx-model-switcher.git mlx-server
cd mlx-server

python -m venv .venv
source .venv/bin/activate
pip install mlx-lm
```

---

## 📋 1. models.conf の設定

`models.conf`（例）：

```properties
# ==============================================
# MLX モデル定義ファイル
# ==============================================

# --- メイン運用モデル（推奨） ---
Qwen3.6-35B-A3B-4bit-DWQ=mlx-community/Qwen3.6-35B-A3B-4bit-DWQ

# ローカルパスも利用可能
my-model=/absolute/path/to/model

```

相対パスは`./models/my-model`のように`./`を付けると、Hugging Face IDと明確に区別できます。

---

## 🔧 2. Hermes Agent 設定例

`custom_providers` に以下のように追加してください：

```yaml
custom_providers:
  - name: local-llm
    base_url: http://localhost:8080/v1
    model: mlx-community/Qwen3.6-35B-A3B-4bit-DWQ   # デフォルトモデル
    api_mode: chat_completions
    models:
      mlx-community/Qwen3.6-35B-A3B-4bit-DWQ:
        context_length: 65536
        max_tokens: 8192
```

**ポイント**
- `models:` 配下のキーは **Hugging Face上のモデル名**（mlx-community/...）を使う
- `context_length: 65536` は実機で長文入力を確認した、現在の安定運用値
- スクリプトも同じHugging Face IDをサーバーへ渡すため、最初のリクエストで同じモデルを再ロードしません
- モデル取得はサーバープロセス内だけオフライン固定です。切り替え先は事前にダウンロードしてください

---

## 🤝 複数アプリから共有する

1つのMLXサーバーを起動し、各アプリのLLM接続先を同じAPIへ向けることで、同じモデルをプロジェクトごとにロードする必要がなくなります。

### 接続の手順

1. このスクリプトで共有するモデルを一度起動します。
2. 各アプリの外部LLM／共有サーバーモードを選び、Base URLを`http://127.0.0.1:8080/v1`に設定します。
3. リクエストの`model`には、起動したモデルと同じHugging Face ID（ローカルモデルの場合は起動時のモデルパス）を指定します。`models.conf`の左辺のエイリアスは起動スクリプト専用です。
4. 各アプリ側のモデル読み込みやLLMサーバー自動起動・終了を無効にします。共有サーバーは起動したターミナルで管理し、終了は`Ctrl+C`で行います。

たとえば、上の設定例のモデルを起動した後は、次のリクエストで接続できます。

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "mlx-community/Qwen3.6-35B-A3B-4bit-DWQ",
    "messages": [{"role": "user", "content": "こんにちは！"}],
    "max_tokens": 128,
    "stream": true
  }'
```

接続設定名や外部サーバーの利用方法は、各アプリのドキュメントを参照してください。

同時利用時の待ち時間は、各アプリとサーバーの同時実行設定に応じて確認してください。

---

## 🚀 使い方

```bash
# 対話メニューから起動
./start_server.sh

# 直接指定して起動
./start_server.sh Qwen3.6-35B-A3B-4bit-DWQ

# 一覧表示
./start_server.sh list
```

---

## ⚙️ スクリプト内設定

現在の標準値：

- リクエストで省略した場合の出力値: 8192 tokens
- クライアント側コンテキスト目標: 65536 tokens
- 同時生成: 2、同時プロンプト読み込み: 1
- 長文の読み込み単位: 512 tokens
- プロンプトキャッシュ上限: 4GB
- 接続先: `127.0.0.1:8080`
- Thinking: 無効

一時的に変更する場合は、`MLX_PORT`、`MLX_MAX_TOKENS`、`MLX_DECODE_CONCURRENCY`、`MLX_PROMPT_CONCURRENCY`、`MLX_PREFILL_STEP_SIZE`、`MLX_PROMPT_CACHE_BYTES`などの環境変数を利用できます。`MLX_MAX_TOKENS`はサーバーの既定値で、APIリクエストに対する強制上限ではありません。

---

## 📌 注意事項

- Apple Silicon + MLX専用
- 大規模モデルはUnified Memoryを十分に確保してください
- 標準ではlocalhost専用です。外部公開は`MLX_HOST`だけではできず、`MLX_ALLOW_REMOTE=1`も必要です
- ブラウザからのCORSは同じlocalhostのサーバーoriginだけを標準で許可します。別ポートのUIを直結する場合は`MLX_ALLOWED_ORIGINS`へ追加してください
- `models.conf`は起動用エイリアスであり、APIリクエストのモデル許可リストではありません。信頼できるローカルクライアント専用です

---

## 📄 License

MIT License

開発に参加する方は [開発者向けガイド](CONTRIBUTING.md) を参照してください。
