# 2026-10-24 GGUF キャッシュ移設

Gemma QAT、画像用 mmproj、Gemma MTP、Qwen125B の2分割本体、Qwen MTP を `~/.cache/gguf/models/` へ同一デバイスの rename で移した。6ファイル計95,671,473,024 bytes。ハードリンク・symlinkや本体複製を作らず、元inodeと全SHA-256が各manifestと一致した。

起動コード・モデルmanifestは `gguf/`、推論runtimeは `gguf/runtime/bin/` を正本とした。モデルの保存先 `MODELS_ROOT` を既存管理コードが読む契約を加え、サイズ表示・削除確認も実体保存先を使用する。従来の外部ランチャーは `MODELS_ROOT` 未指定なら従来の `models/` 配下を使用する。新しい起動APIは追加していない。

`Desktop/tools/実験/MLX 8080.app` を既存の `tools/build_apps.py` で再ビルドし、`MLXServerRoot=~/mlx-server`、`MLXGGUFRoot=~/mlx-server/gguf` に接続した。既定モデルは Gemma GGUF のまま。既存の `models.conf` と管理設定 `models.json` はバイト単位で保持した。配置前のHEADは `50db0afc330e927f9a8b21550ef7a319409e3a41`、変更前から存在した `models.conf` の差分はこの作業に含めない。

## 検証

- Gemma用の元3ファイルは計15,694,045,216 bytes、Qwen125B＋MTPは計79,977,427,808 bytes。全ハッシュ一致。
- llama-server と必要な9ライブラリを実体コピーし、旧研究フォルダの `LC_RPATH` を `@loader_path` へ変更。10ファイルすべて再署名・検証済み。`--version` 成功。実動作中のロード済みファイルに旧研究フォルダへの依存なし。
- Python 119 tests、JavaScript 20 tests、shell構文、変更対象のdiff検査が成功。サンドボックス内で拒否されたHTTP bindと `/dev/fd` の既存テストは権限付きで実行し、全件通過した。既存 `models.conf` の末尾空行によるdiff警告は変更せず維持。
- 再ビルドしたアプリ内の管理サーバーで2モデルのサイズとGemmaの既定指定を確認。既存 `/api/start` 経由でGemmaを起動し、`/health` は `ok`、`/v1/chat/completions` は「接続できました」と応答。
- Gemmaはキャッシュ内の本体・mmproj・MTPを使用。`--spec-type draft-mtp --spec-draft-n-max 2`、context 65536を保持。応答の `draft_n=2`、`draft_n_accepted=1` とdraftロードログを確認した。これは動作確認であり速度比較ではない。
- 既存 `/api/stop` でGemmaを停止。検証用管理プロセスも停止し、8080/8766の待受解放を確認した。最後の検証PIDは llama-server 32699、manager 32672（いずれも終了済み）。
- Qwen125Bは全ファイルのハッシュ・サイズ・設定を確認した。今回モデルのロード・生成は実施していない。

移設記録は `gguf/evidence/cache-migration-2026-10-24.json`、実動作記録は `gguf/evidence/cache-migration-smoke-2026-10-24.json`。これらとログ・バイナリはローカルのみ。検証スクリプト初回はログ表現の期待が合わず、成功した推論後に検証判定だけ失敗したが、finally処理で停止・ポート解放した。実際のdraftロードと生成カウンタを使う検証に修正し、API停止まで通過した。

## 起動・再構成

通常は `Desktop/tools/実験/MLX 8080.app` を開き、既定のGemmaを起動する。終了時点ではサーバーを起動したままにしていない。

起動経路の再構成に必要な upstream 基点、採用済み55コミット、未採用の追加ローカルコミット、元ビルド設定は `gguf/runtime/` に保存した。`source-patches.bundle` は `git bundle verify` 済み。再ビルド自体は今回行っていない。手順は `gguf/runtime/README.md`。
