# 採用済み GGUF runtime の再構成資料

現在の `bin/` は検証済み build-14a の llama-server と必要な9ライブラリ。ビルド元は llama.cpp `6190802c8d3391ee908fb254d12fc243ad4540c6`。移設では再コンパイルせず、全 Mach-O の `LC_RPATH` を `@loader_path` へ変更して ad hoc 再署名した。元・移設後の SHA-256 と依存名は `../runtime-manifest.json`。

`source-patches.bundle` は upstream base `781dbc5ac98921dbdb5e5b2ec5b7a50960e937d4` を前提とし、採用済み55コミットと、その後の未採用ローカル変更 `93cf9d707cb35bf7e6d63e29a352cf3fb4e96771` を保持する。後者は Gemma MTP の request reset 修正であり、現在の実行ファイルに含まれるとみなさない。巨大な upstream 履歴やモデル本体は含めていない。

再構成は専用の作業ディレクトリで行う。次の bundle パスを、このディレクトリの絶対パスへ置き換える。

```sh
git init llama.cpp
git -C llama.cpp remote add origin https://github.com/ggml-org/llama.cpp.git
git -C llama.cpp fetch --depth=1 origin 781dbc5ac98921dbdb5e5b2ec5b7a50960e937d4
git -C llama.cpp fetch /absolute/path/to/gguf/runtime/source-patches.bundle HEAD
git -C llama.cpp checkout --detach 6190802c8d3391ee908fb254d12fc243ad4540c6
cmake -S llama.cpp -B llama.cpp/build-14a -DCMAKE_BUILD_TYPE=Release -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON -DLLAMA_CURL=OFF
cmake --build llama.cpp/build-14a --target llama-server -j 12
```

元の全ターゲット・パッチハッシュ・コンパイラ情報は `build-config.json`、CMake設定の採録は `cmake-options.txt`。macOS arm64 と Homebrew OpenSSL 3 を使用する。コンパイラと SDK の変更後に同じバイナリハッシュになることは検証していない。

再配置時は manifest にある10個を build 出力から実体コピーし、各ファイルを `otool -L/-l` で確認する。元 build ディレクトリを指す `LC_RPATH` は `install_name_tool -rpath ORIGINAL_BIN @loader_path FILE` で変更し、各ファイルを `codesign --force --sign - FILE`、`codesign --verify --strict FILE` で再署名・検証する。モデルは `~/.cache/gguf/models/` を使用する。起動前に manifest のサイズ・ハッシュと必要な補助モデルを確認し、管理アプリの既存経路で検証する。
