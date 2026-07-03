```markdown
# MLX Server 起動スクリプト（モデル切り替え対応版）

Apple Silicon（MLX）向け**ローカルLLMサーバー**を快適に運用するためのBash起動スクリプトです。

矢印キーだけでモデルを選択できる対話型メニューと、`models.conf`によるエイリアス管理で、複数のモデルをサクサク切り替えられます。  
**Hermes Agent**からモデルの切り替えができます。

---

## ✨ 主な特徴

- **矢印キー選択メニュー**（↑↓ + Enterのみ）
- `models.conf` でエイリアス管理
- `snapshots/latest` 自動解決
- Hermes Agent 完全対応（動的モデル切り替え）
- 設定ファイルにコメントで整理しやすい

---

## 📁 インストール

```bash
git clone https://github.com/yukincom/mlx-server.git
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

# --- メイン運用モデル ---
Qwen3.6-35B-A3B-4bit-DWQ=~/.cache/huggingface/hub/models--mlx-community--Qwen3.6-35B-A3B-4bit-DWQ/snapshots/latest

# --- コーディング用モデル ---
Qwen3.6-27B-6bit=~/.cache/huggingface/hub/models--mlx-community--Qwen3.6-27B-6bit/snapshots/latest

```

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
        context_length: 131072
        max_tokens: 8192
      mlx-community/Qwen3.6-27B-6bit:
        context_length: 131072
        max_tokens: 8192
```

**ポイント**
- `models:` 配下のキーは **Hugging Face上のモデル名**（mlx-community/...）を使う
- スクリプトのエイリアス（`Qwen3.6-35B-A3B-4bit-DWQ`）とは別なので、両方を意識して設定
- Hermes Agent側でモデル切り替えをすると、サーバー側の選択メニューで起動したモデルも動的に変わります

---

## 🚀 使い方

```bash
# シンプルなコードで起動
./start_server.sh

# 直接指定して起動
./start_server.sh Qwen3.6-35B-A3B-4bit-DWQ

# 一覧表示
./start_server.sh list
```

---

## ⚙️ スクリプト内設定

`start_server.sh` 上部で調整可能：
- `MAX_TOKENS=8192`
- `CONTEXT_LENGTH=65536`
- `HOST="127.0.0.1"`
- `PORT="8080"`

---

## 📌 注意事項

- Apple Silicon + MLX専用
- 大規模モデルはUnified Memoryを十分に確保してください
- 現在はlocalhost専用です

---

## 📄 License

MIT License
