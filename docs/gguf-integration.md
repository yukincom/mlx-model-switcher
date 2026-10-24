# GGUFとの連携

Swift管理サーバーが `profiles.json` を読み、指定したllama.cpp実行ファイルを直接起動する。モデル一覧・起動停止・ファイル検証・削除保護にPythonは使わない。標準セットアップではGGUF連携は無効。

```bash
python3 tools/build_apps.py --output /Applications --gguf-root /absolute/path/to/gguf
```

設定例は [`examples/gguf/profiles.json`](../examples/gguf/profiles.json)。指定ディレクトリへ `profiles.json` として置く。モデル重みと推論実行ファイルの導入・更新は別途行う。

- `models_root`: 重みの保存先。絶対パスまたは `~/` で指定する。
- `binary`: 設定ディレクトリ内のllama.cpp実行ファイルへの相対パス。
- `profiles`: `target`, `alias`, `model`, `name`, `manifest`, `model_file`, `arguments`, `required_verifications` を持つモデル記述。
- `manifest`: 設定ディレクトリ内のJSONへの相対パス。`files` 配列の各項目は `directory`, `file`, `bytes` を持ち、重みは `models_root/directory/file` に置く。分割GGUF、mmproj、draftも全て含める。
- `model_file`: manifest内で一意になるメインモデルのファイル名。ディレクトリ部分は含めない。
- `arguments`: 追加引数の配列。`{model:ファイル名}` はmanifestで検証したファイルの絶対パスに置換する。mmproj・draftもこの形式で指定する。
- `required_verifications`: 事前検証の記録ファイルへの相対パスの配列。起動時に存在を検査し、重みはmanifestのサイズと照合する。
- `environment` / `unset_environment`: モデル固有の環境変数の設定・解除。先読みモデルの設定を別モデルへ引き継がない場合に用いる。

モデル、Thinking、host、port、API上のaliasは管理側が設定するため `arguments` では上書きできない。起動記録は `evidence/llama-server.json` / `evidence/managed-server.json`、ログは `logs/llama-server.log` に保存し、その変更とPID終了を画面へ通知する。

削除確認にはモデル別manifestを使い、パストラバーサルと親ディレクトリを含むsymlinkを拒否する。使用中の重み、mmproj、draft、共有ファイルを保護し、確認後にファイルや設定が変わった場合は削除しない。

採用済みのローカル起動設定は `gguf/profiles.json`、モデル本体は `~/.cache/gguf/models/` に分離する。推論実行ファイルは `gguf/runtime/bin/`、由来とハッシュは `gguf/runtime-manifest.json` に記録する。実行ファイルとモデルはGit管理に含めない。構成を指定してビルドする例は `--gguf-root "$PWD/gguf"`。
