# 外部GGUFランチャーとの連携

GGUFの起動設定を管理アプリ内へ複製せず、指定した外部ランチャーの `server.py` を利用する。標準セットアップでは無効。一般的な任意のGGUFファイルを選べば起動できる機能ではない。

```bash
.venv/bin/python tools/build_apps.py --gguf-root /absolute/path/to/launcher
```

指定先のPythonコードをモデル操作worker内で実行するため、自分が管理する信頼済みランチャーだけを設定する。モデル重みや推論実行ファイルの導入・更新は連携先が担う。

連携先には次の契約が必要。

- `gguf_profiles()`が `alias`, `model`, `name`, `manifest`, `target` を持つモデル記述を返す。
- `start_gguf_if_idle(target, thinking=bool)`が排他・空きポート・モデル検証を行い、独立した推論プロセスのPIDを返す。
- `RECORD`と `is_our_llama(current)`で起動記録とプロセスの同一性を照合できる。`current`は `pid`, `command` を持つdictで、記録には `target` が必須。
- manifestの `files` は `directory`, `file`, `bytes` を持つ。重みの保存先はランチャーの絶対パス `MODELS_ROOT`。省略時は従来どおり連携先の `models/` を使う。パスの親を含むsymlinkは削除対象にできない。
- 起動記録は `evidence/llama-server.json` / `evidence/managed-server.json`、ログは `logs/llama-server.log` に書く。これらの変更をSwift側でも監視する。

モデル別manifestを削除確認に使用する。パストラバーサルやsymlinkを拒否し、使用中の重み、mmproj、draft model、共有ファイルを削除対象にしない。APIの詳細な呼出形式は `src/mlx_activation_lab/manager_models.py` が正本。

採用済みのローカル起動設定は `gguf/server.py` に置き、モデル本体は `~/.cache/gguf/models/` に分離する。ローカルの推論実行ファイルは `gguf/runtime/bin/`、由来とハッシュは `gguf/runtime-manifest.json` に記録する。実行ファイルとモデルはGit管理に含めない。アプリをこの構成でビルドするときは `--gguf-root "$PWD/gguf"` を指定する。
