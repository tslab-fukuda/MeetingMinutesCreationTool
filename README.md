# MeetingMinutesCreationTool

会議の録音、ライブ文字起こし、議事録 TeX 編集をまとめて扱うためのローカル Web アプリです。  
Windows 上で動かすことを前提にしています。

## できること

- マイクから会議音声を録音する
- 録音しながら区間ごとに文字起こしする
- ライブ文字起こし結果を画面右側で確認する
- 議事録用の TeX ファイルを画面左側で編集する
- 文字起こし結果を TeX 内の自動管理ブロックへ追記する
- `latexmk` を使って TeX をコンパイルする

## 想定環境

- Windows
- Python 3.11 以上
- マイク入力が使えること
- TeX コンパイルも使う場合:
  - `latexmk`
  - `uplatex`
  - `dvipdfmx`

補足:

- 録音とローカル文字起こしだけなら Python 環境があれば動きます。
- TeX の PDF コンパイル機能を使うには、日本語 TeX 環境が別途必要です。

## クローン後の一発セットアップ

このリポジトリをクローンしたあと、プロジェクト直下で次を実行してください。

```powershell
.\setup_windows.bat
```

このバッチで行うこと:

- Python 3.11 の確認
- Python が見つからない場合は `winget` でインストールを試行
- `.venv` 仮想環境の作成
- `requirements.txt` のインストール
- `recordings/` と `transcripts/` の作成
- `latexmk` / `uplatex` / `dvipdfmx` の存在確認
- TeX コマンドが見つからない場合は `setup_texlive.bat` を自動実行

`setup_texlive.bat` では次を行います:

- 公式 CTAN ミラーから TeX Live インストーラを取得
- `C:\texlive\2026` へ TeX Live を無人インストール
- このプロジェクトで必要な日本語 TeX 関連パッケージを追加
- `latexmk` / `uplatex` / `dvipdfmx` が使える状態にする

注意:

- `winget` で Python を新規インストールした直後は、環境によっては新しいターミナルで再実行が必要な場合があります。
- `setup_texlive.bat` は `curl` と `tar` を使います。通常の Windows 10/11 では標準搭載されている想定です。
- TeX Live のインストーラ取得やパッケージ展開には時間とディスク容量が必要です。
- `winget` が使えない端末でも、TeX については `setup_texlive.bat` で別途導入できます。

## Git に含めないもの

開発環境構築でローカルに生成・インストールされるものは Git に含めない前提です。

- `.venv/`
- `recordings/`
- `transcripts/`
- `.tmp_texlive/`
- LaTeX の生成物
- ローカルの起動ログ

そのため、クローンまたは fork した先で各自が `setup_windows.bat` を実行して環境構築する運用を想定しています。

## Web アプリの起動

セットアップ後は次で起動できます。

```powershell
.\run_webapp.bat
```

または手動で:

```powershell
.\.venv\Scripts\python.exe -m uvicorn webapp.server:app --reload
```

起動後、ブラウザで以下を開いてください。

```text
http://127.0.0.1:8000
```

補足:

- Web アプリの TeX コンパイルは、`PATH` に TeX Live が未反映でも `C:\texlive\2026\bin\windows` などの標準配置を自動検出します。
- 初回セットアップは `.\setup_windows.bat` のみで進められるようにしてあります。

## 画面構成

- 画面上部:
  - 録音開始
  - 停止
  - TeX 保存
  - TeX 再コンパイル
  - 入力デバイス選択
  - 自動反映 ON/OFF
- 左ペイン:
  - 議事録 TeX エディタ
- 右ペイン:
  - ライブ文字起こし
- 下部:
  - システムログ
  - コンパイルログ

## 主なファイル

- `webapp/server.py`
  - FastAPI バックエンド
- `webapp/static/`
  - Web UI
- `tools/record_audio.py`
  - 単体録音ツール
- `tools/transcribe_audio.py`
  - OpenAI Audio API を使う単体文字起こしツール
- `tools/monitor_meeting.py`
  - 録音と文字起こしの同時監視ツール
- `Texテンプレート2026/tmplate.tex`
  - 編集対象の議事録テンプレート

## 録音・文字起こしツール単体の使い方

詳しくは [RECORDING.md](./RECORDING.md) を参照してください。

## OpenAI API キー

OpenAI Audio API を使う場合は `OPENAI_API_KEY` が必要です。

PowerShell 例:

```powershell
$env:OPENAI_API_KEY="your_api_key_here"
```

補足:

- API の利用料金は ChatGPT の通常契約とは別です。
- API のクォータ不足時は、ローカル文字起こしに切り替えて使えます。

## 依存パッケージ

Python 側の依存は `requirements.txt` にまとまっています。

- `numpy`
- `sounddevice`
- `openai`
- `faster-whisper`
- `fastapi`
- `uvicorn`

## TeX コンパイルについて

このプロジェクトの TeX は日本語向けの設定を前提にしています。  
そのため、PDF 生成まで使う場合は `uplatex` と `dvipdfmx` を含む日本語 TeX 環境が必要です。

ローカルですでに TeX Live が入っている場合は、そのまま使えることが多いです。
未導入なら `.\setup_windows.bat` から自動で `setup_texlive.bat` が呼ばれます。TeX だけ個別に入れたい場合は `.\setup_texlive.bat` を単独で実行してください。

## トラブルシュート

### 1. `setup_windows.bat` 実行時に Python が見つからない

- `winget` が使える環境なら自動インストールを試します
- それでも失敗する場合は Python 3.11 以上を手動インストールしてから再実行してください

### 2. 録音は始まるが音が入らない

- 入力デバイスを見直してください
- マイクの OS 権限を確認してください
- ライブ監視中の入力レベル表示を確認してください

### 3. TeX コンパイルに失敗する

- `latexmk`, `uplatex`, `dvipdfmx` が使えるか確認してください
- 日本語 TeX 環境が正しく入っているか確認してください
- 未導入なら `.\setup_windows.bat` を再実行してください
- 既に導入済みでも古いターミナルでは `PATH` が未反映なことがあるため、新しいターミナルで再起動してください

## 補足

このリポジトリでは、録音ファイル、文字起こし結果、LaTeX の生成物は `.gitignore` で除外しています。  
そのため、クローン先では必要に応じて自分で生成して使う形になります。開発環境構築に伴う一時ファイルやインストーラ展開物も Git には含めません。
