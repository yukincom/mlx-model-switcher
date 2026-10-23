# 開発者向けガイド

## テスト

管理アプリを含むテストにはPython 3.11以降、Node.js、`huggingface-hub`が必要です。macOSではSwiftコンパイラも使用します。MLXやモデル重みは不要です。

```bash
/bin/bash -n start_server.sh
python3 -m pip install 'huggingface-hub==1.12.0'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
node --test tests/*.cjs
```

テストは一時ディレクトリ内のダミーモデル、代替サーバー、実際にコンパイルしたSwift管理サーバーを使用します。モデルのダウンロードや実際のMLXサーバー起動は行いません。

検証対象：

- `/bin/bash`による構文チェック
- モデル一覧、引数、空白を含むパスの処理
- Hugging Faceキャッシュの解決と、不完全・曖昧なキャッシュの拒否
- 起動引数、オフライン設定、設定値の検証
- ローカル起動コマンドへの引数転送と終了コードの保持
- ストリーミング応答の時間計測と、途中切断の検出
- 削除対象・共有ファイル・使用中モデル・確認fingerprintの検証
- 管理HTTPのtoken、Host/Origin、body上限、要求の直列化、worker終了
- SSE、ファイル変更、PID終了、ウィンドウの表示状態に応じた更新
- 起動スクリプトの起動記録と、定期ポーリングを使わないUI

## CI

[GitHub Actions](.github/workflows/ci.yml)で、push・Pull Request・手動実行時にLinuxとmacOSでPython・JavaScriptを検証します。macOSではSwiftのHTTP/SSE・OS通知テストとアプリbundleのビルド・署名検証も実行します。

MLXによる推論速度、音声、各アプリや実機との接続はCIの検証対象外です。これらに影響する変更では、必要な実環境で別途確認してください。

## アプリのビルド

```bash
python3 tools/build_apps.py --python /absolute/path/to/venv/bin/python3
```

出力は `dist/MLX 8080.app`。`--output`で配置先、`--server-root`で既存の `start_server.sh` と `models.conf` のあるディレクトリを指定できます。モデル操作用Pythonはsymlinkの参照先へ展開せず、venv内のパスを保持します。生成物はローカルのad hoc署名付きで、Developer ID署名・公証・配布用リリースの生成は行いません。

コード・画面はbundleへコピーされます。変更後は実アプリを終了してから再ビルドします。既存アプリを置き換える前にbundle IDと実行状態を検査し、ステージした成果物を同一ファイルシステム内でrenameします。

`src/mlx_activation_lab`は管理部分を抽出した既存パッケージ名です。実験ラボ本体への実行時依存はありません。

## 設定・連携を変更するとき

- `context_length`はMLXサーバーの起動引数ではありません。Hermesなどのクライアント設定との整合を確認してください。
- 起動時に読み込む設定の変更は、次回のサーバー起動から反映されます。
- エージェントの権限・確認・ツール制限は、各クライアント側で管理します。

## ローカル起動コマンド

サーバーディレクトリに実行可能な`local_server_entry`がある場合、`mlx_lm.server`の代わりに呼び出します。モデルやポートなどの引数とオフライン設定は、そのまま渡されます。
ラッパーで追加処理を行う場合は、受け取った引数を`"$@"`でMLX-LMへ渡してください。終了コードとシグナルを保つため、最後のコマンドは`exec`で起動します。
ファイルがない場合や実行権限がない場合は、通常の`mlx_lm.server`で起動します。マシン固有のファイルとしてGitの追跡対象から除外しています。

## 初回応答の計測

`tools/probe_ttft.py`で最初のトークンまでの時間とキャッシュ使用量を計測できます。Python標準ライブラリだけで動き、入力文や応答本文は出力に保存しません。
`request.json`には通常の`/v1/chat/completions`リクエストを用意してください。

```bash
python3 tools/probe_ttft.py --input-json request.json --repeat 2
```

`first_token_ms`は推論・ツール呼び出しも含む最初の出力、`first_content_ms`は本文の開始です。
`usage.prompt_tokens_details.cached_tokens`と入力tokensを比べると、入力の再処理を見分けられます。途中で切れたストリームは正常な計測結果として扱いません。
計測も実際に推論を実行するため、共有サーバーが空いているときに行ってください。

Qwenのハイブリッドキャッシュでは、会話途中の状態を任意の位置へ巻き戻せません。共通入力の状態を別に保持することが、新規会話や会話の分岐後の速度に影響します。
mlx-lm 0.31.3の容量整理はリクエスト投入時に行われ、処理中の増加まで常に制限するものではありません。キャッシュ容量設定はプロセス全体のメモリ上限ではありません。
モデル仕様上のコンテキスト長と、実際に処理できる長さ・速度・メモリ使用量は、利用環境で別途確認してください。

## 起動設定

アプリは `127.0.0.1:8080` とビルド時の `server-root/models.conf` を使用し、継承されたCLI用host・port・設定ファイルの値では変更されません。CLIの既定値は以下です。

| 項目 | 通常 | Qwen3.8-27B |
| --- | --- | --- |
| 省略時の出力tokens | 8192 | 8192 |
| クライアント側コンテキスト目標 | 65536 | 262144 |
| 同時生成 / 同時prompt処理 | 2 / 1 | 1 / 1 |
| prefill単位 | 2048 | 2048 |
| prompt cache設定 | 4GB | 8GB |

`MLX_PORT`, `MLX_MAX_TOKENS`, `MLX_DECODE_CONCURRENCY`, `MLX_PROMPT_CONCURRENCY`, `MLX_PREFILL_STEP_SIZE`, `MLX_PROMPT_CACHE_BYTES`, `MLX_THINKING`で上書きできます。`MLX_MAX_TOKENS`は省略時の値で、APIリクエストへの強制上限ではありません。Thinkingは `on/off`、Qwen3.8-27Bでは `low/medium/xhigh`も指定できます。GUIはON/OFFを選択します。

CLIでloopback以外へbindする場合は `MLX_HOST`と `MLX_ALLOW_REMOTE=1`の両方が必要です。CORSの許可originは `MLX_ALLOWED_ORIGINS`で指定します。`models.conf`は起動用の設定であり、推論APIのモデル許可リストではありません。標準構成は信頼するローカルクライアント向けです。
