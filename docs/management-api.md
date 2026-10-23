# 管理API

`MLX 8080.app`が起動している間、管理APIを `http://127.0.0.1:8766` で利用できる。推論用の8080とは別のポートである。

`GET /api/health`は `app: mlx-server-manager`、`backend: swift`、`protocol: 1` を返す。`GET /api/status`はモデル一覧、現在のPID・コマンド、操作用tokenを返す。tokenは管理プロセスごとに生成されるため保存・固定しない。POSTには現在のtokenを `X-Server-Token` ヘッダーで付け、JSON objectを送る。

| 操作 | メソッド・パス | JSON |
| --- | --- | --- |
| 起動 | `POST /api/start` | `{"alias":"登録済みalias","thinking":false}` |
| 停止 | `POST /api/stop` | statusから取得した `pid` と `command` |
| 表示名変更 | `POST /api/rename` | `alias`, `name` |
| デフォルト | `POST /api/default` | `alias` |
| 保存済みモデル一覧 | `GET /api/cached-models` | — |
| キャッシュから登録 | `POST /api/register` | `model` |
| 削除確認 | `POST /api/delete-preview` | `aliases`, `body` |
| 削除実行 | `POST /api/delete` | 同じ `aliases`, `body` と確認時の `fingerprint` |

`body: false`は起動一覧からの解除、`true`はモデル本体の削除。削除実行前にpreviewの対象を確認する。対象が変化した場合は再確認が必要になる。

停止では表示時のPIDとコマンドの完全一致を再検査する。未知のプロセス、PIDの入れ替わり、確認できない使用状態にはシグナルを送らない。強制killは行わない。

`GET /api/events`はSSE接続で、変更時に `event: changed` を送る。接続時と変更通知時に必要な状態を取得し、定期ポーリングはしない。CLIも共通の起動記録を書き、ログ変更とPID終了通知で画面へ反映する。管理API・起動スクリプト・設定したGGUFランチャーを全く通らない任意のプロセス起動までは監視しない。

管理APIはloopbackだけにbindし、Host/Origin、POST token、リクエストサイズを検査する。同じユーザー権限のローカルプログラムに対する認証境界ではない。外部公開用のサービスとしては設計していない。
