# MLX Server 起動スクリプト（モデル切り替え対応版）

Apple Silicon（MLX）向け**ローカルLLMサーバー**を快適に運用するためのBash起動スクリプトです。

矢印キーだけでモデルを選択できる対話型メニューと、`models.conf`によるエイリアス管理で、複数のモデルを切り替えられます。
OpenAI互換APIとして、**Hermes Agent**などのローカルクライアントから利用できます。

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
Qwen3.8-27B-8bit=mlx-community/Qwen3.8-27B-8bit

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
    model: mlx-community/Qwen3.8-27B-8bit   # デフォルトモデル
    api_mode: chat_completions
    models:
      mlx-community/Qwen3.8-27B-8bit:
        context_length: 262144
        max_tokens: 8192
```

**ポイント**
- `models:` 配下のキーは **Hugging Face上のモデル名**（mlx-community/...）を使う
- Qwen3.8-27B-8bitの`context_length: 262144`は配布モデルの`text_config.max_position_embeddings`に合わせる。Hermesの最低条件は64,000 tokensであり、モデル自体の上限とは異なる。全長での実行速度・メモリ使用量は実機検証が必要
- スクリプトも同じHugging Face IDをサーバーへ渡すため、最初のリクエストで同じモデルを再ロードしません
- モデル取得はサーバープロセス内だけオフライン固定です。切り替え先は事前にダウンロードしてください

---

## 🚀 使い方

```bash
# 対話メニューから起動
./start_server.sh

# 直接指定して起動
./start_server.sh Qwen3.8-27B-8bit

# コーディングや難しい判断でThinkingを使う
MLX_THINKING=medium ./start_server.sh Qwen3.8-27B-8bit

# 一覧表示
./start_server.sh list
```

---

## ⚙️ スクリプト内設定

現在の標準値：

- リクエストで省略した場合の出力値: 8192 tokens
- クライアント側コンテキスト目標: 通常65536、Qwen3.8-27Bはモデル仕様に合わせた262144 tokens
- 同時生成: 通常2、Qwen3.8-27Bは1。同時プロンプト読み込みは1
- 長文の読み込み単位: 2048 tokens（mlx-lm 0.31.3の標準値）
- プロンプトキャッシュ容量設定: 4GB。Qwen3.8でも共通入力と処理中のキャッシュを保持する
- 接続先: `127.0.0.1:8080`
- Thinking: 無効（既定）。Qwen3.8は`MLX_THINKING=low|medium|xhigh`で切り替え可能

一時的に変更する場合は、`MLX_PORT`、`MLX_MAX_TOKENS`、`MLX_DECODE_CONCURRENCY`、`MLX_PROMPT_CONCURRENCY`、`MLX_PREFILL_STEP_SIZE`、`MLX_PROMPT_CACHE_BYTES`、`MLX_THINKING`などの環境変数を利用できます。`MLX_MAX_TOKENS`はサーバーの既定値で、APIリクエストに対する強制上限ではありません。

Qwen3.8の公式チャットテンプレートはXML形式のツール呼び出しを生成し、対応するMLX-LMはそれをOpenAI互換の`tool_calls`へ変換します。Hermesのツール実行にはこの変換が必要ですが、PC操作の権限や確認ルールを変更するものではありません。モデル更新後は実際のツール呼び出しまで確認してください。

### 最初の応答が遅い場合

`tools/probe_ttft.py`で最初のトークンまでの時間とキャッシュ使用量を計測できます。
Python標準ライブラリだけで動き、入力文や応答本文は出力に保存しません。
`request.json`には通常の`/v1/chat/completions`リクエストを用意してください。

```bash
python3 tools/probe_ttft.py --input-json request.json --repeat 2
```

`first_token_ms`は推論・ツール呼び出しも含む最初の出力、`first_content_ms`は本文の開始です。
`usage.prompt_tokens_details.cached_tokens`と入力tokensを比べると、入力の再処理を見分けられます。
計測も実際に推論を実行するため、共有サーバーが空いているときに行ってください。

Qwenのハイブリッドキャッシュでは、会話途中の状態を任意の位置へ巻き戻せません。
共通入力の状態を別に保持することが、新規会話や裏側の振り返り後の速度に影響します。
mlx-lm 0.31.3の容量整理はリクエスト投入時に行われ、処理中の増加まで常に制限するものではありません。
4GBはプロセス全体のメモリ上限ではなく、長い会話や複数エージェントでの利用には別途実測が必要です。

---

## 📌 注意事項

- Apple Silicon + MLX専用
- 大規模モデルはUnified Memoryを十分に確保してください
- 標準ではlocalhost専用です。外部公開は`MLX_HOST`だけではできず、`MLX_ALLOW_REMOTE=1`も必要です
- ブラウザからのCORSは同じlocalhostのサーバーoriginだけを標準で許可します。別ポートのUIを直結する場合は`MLX_ALLOWED_ORIGINS`へ追加してください
- `models.conf`は起動用エイリアスであり、APIリクエストのモデル許可リストではありません。信頼できるローカルクライアント専用です
- パソコン操作エージェントの安全境界はこのサーバーではなく、エージェント側の権限・確認・ツール制限で設けてください
- `context_length`はMLXサーバーの起動引数ではなく、Hermesなど各クライアント側でも設定してください
- 設定変更は次回のサーバー起動から反映されます

---

## 📄 License

MIT License
