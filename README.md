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

## ブランチへ変更を積む

開発中の変更は `main` へ直接 push せず、作業ブランチへ積みます。

```powershell
.\push_branch.bat "コミットメッセージ"
```

このバッチで行うこと:

- `origin` の最新状態を取得
- `main` 上で実行した場合は作業ブランチを自動作成
- ローカル変更を `git add -A` してコミット
- `origin/main` を現在の作業ブランチへマージ
- 現在の作業ブランチだけを `origin` に push

ある程度機能がまとまったら、次の Pull Request 用バッチで `main` への反映準備をします。`main` へ直接 push する運用ではなく、Pull Request を作ってレビュー後に merge する想定です。

## 変更を Pull Request にする

変更をコミットして Pull Request を作る一連の作業は、次のバッチで実行できます。

```powershell
.\publish_pr.bat "コミットメッセージ" "Pull Request タイトル"
```

このバッチで行うこと:

- `origin` の最新状態を取得
- `main` 上で実行した場合は作業ブランチを自動作成
- ローカル変更を `git add -A` してコミット
- `origin/main` を現在の作業ブランチへマージ
- 作業ブランチを `origin` に push
- GitHub CLI または Git の保存済み GitHub 認証情報を使って Pull Request を作成

注意:

- 事前に GitHub へ push できる認証状態にしておいてください。
- マージ競合が起きた場合は、競合を解消してコミットしてから再実行してください。
- 生成物は `.gitignore` に従って除外されるため、必要なソース変更だけがコミット対象になります。
- 日々の小さな変更は `push_branch.bat` で作業ブランチへ push し、まとまった段階で `publish_pr.bat` を使ってください。

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
  - 選択セクションへ要約反映
  - TeX 保存
  - TeX 再コンパイル
  - 入力デバイス選択
  - 自動反映 ON/OFF
  - 即時要約 ON/OFF
- 左ペイン:
  - 議事録 TeX エディタ
- 右ペイン:
  - ライブ文字起こし
  - 要約の挿入セクション選択
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
- `local_settings.env.example`
  - OpenAI / ローカル LLM のローカル設定テンプレート
- `push_branch.bat`
  - 開発中の変更を作業ブランチへコミットして push する補助バッチ
- `publish_pr.bat`
  - 変更のコミット、push、Pull Request 作成をまとめて行う補助バッチ
- `Texテンプレート2026/tmplate.tex`
  - 元になる議事録テンプレート。Web アプリから直接書き換えません。
- `Texテンプレート2026/tmplate_minutesYYYYMMDD.tex`
  - Web アプリが日付ごとに作成・編集する議事録ファイル

## 録音・文字起こしツール単体の使い方

詳しくは [RECORDING.md](./RECORDING.md) を参照してください。

## Web アプリの議事録ファイル

Web アプリは `Texテンプレート2026/tmplate.tex` を元テンプレートとして読み込み、起動日の `Texテンプレート2026/tmplate_minutesYYYYMMDD.tex` を作成して編集します。

補足:

- `tmplate.tex` には自動文字起こしログを書き込みません。
- 日付を固定したい場合は `local_settings.env` に `MEETING_MINUTES_DATE=20260428` のように指定できます。
- 生成された PDF や LaTeX の中間ファイルは `.gitignore` で除外されるため、Git にコミットしません。

## Web アプリでの選択セクションへの要約反映

Web アプリ右側のライブ文字起こしには、録音された内容をそのまま表示します。
右側の `挿入セクション` で入力先を手動選択できます。
`選択セクションへ要約反映` を押すと、現在のライブ文字起こし全体を LLM で簡単に要約し、日付別 TeX ファイルの選択中セクション内にある既存の空の `\item` 行へ上から順に入力します。
`即時要約` が ON の場合は、15 秒ごとの文字起こしセグメントが届くたびに自動で要約を更新します。

補足:

- 手入力した会話文を要約する入力欄は使いません。
- 要約結果は内容に応じて複数の `\item` に分かれます。
- 即時要約では、前回までの要約候補と直近の文字起こし文脈を LLM に渡し、15 秒区切りで途切れた内容が同じ話題なら既存項目へ統合します。
- 空の `\item` が足りない場合は、最後に使った空欄の直後へ同じインデントで `\item` を追加します。
- セクションが変わる場合は、まず `挿入セクション` を手動で切り替えます。切り替え後も、そのセクション内では従来通り `\item` が自動で増えます。
- 元テンプレートの `Texテンプレート2026/tmplate.tex` は直接書き換えません。
- 右側の `選択セクションへの挿入先` には、入力予定の空の `\item` 行番号、要約対象の文字起こし件数が表示されます。

## OpenAI API キー

OpenAI Audio API や OpenAI の LLM を使う場合は `OPENAI_API_KEY` が必要です。

設定場所は、リポジトリ直下の `local_settings.env` です。`setup_windows.bat` 実行時に `local_settings.env.example` から自動作成されます。

```text
OPENAI_API_KEY=your_api_key_here
OPENAI_LLM_MODEL=gpt-4o-mini
```

一時的に PowerShell で設定する場合:

```powershell
$env:OPENAI_API_KEY="your_api_key_here"
```

補足:

- API の利用料金は ChatGPT の通常契約とは別です。
- API のクォータ不足時は、ローカル文字起こしに切り替えて使えます。

## LLM 設定

Web アプリでは、文字起こし方式の選択欄から `ChatGPT` または `ローカルLLM` を選んで API の種類を切り替えられます。

OpenAI / ChatGPT API を使う場合:

```text
OPENAI_API_KEY=your_api_key_here
OPENAI_LLM_MODEL=gpt-4o-mini
OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe
```

ローカル推論の OpenAI 互換 API を使う場合:

```text
LOCAL_LLM_BASE_URL=http://127.0.0.1:8000/v1
LOCAL_LLM_MODEL=openai/gpt-oss-120b
LOCAL_TRANSCRIBE_MODEL=openai/gpt-oss-120b
LOCAL_LLM_API_KEY=your_local_api_key_here
```

次の短い名前でも同じ設定として読み込まれます:

```text
BASE_URL=http://127.0.0.1:8000/v1
MODEL=openai/gpt-oss-120b
API_KEY=your_local_api_key_here
```

初期選択を変える場合:

```text
LLM_PROVIDER=openai
```

または:

```text
LLM_PROVIDER=local
```

補足:

- `local_settings.env` は `.gitignore` で除外されるため、API キーは Git に上がりません。
- ローカル推論 API の `BASE_URL` は、OpenAI 互換エンドポイントの `/v1` まで含めて指定してください。
- OpenAI API は Chat Completions 互換の形式で呼び出します。ローカル推論 API も同じ形式に対応している必要があります。
- 録音の文字起こしで `ローカルLLM` を選ぶ場合は、ローカル推論 API が OpenAI 互換の Audio Transcriptions にも対応している必要があります。

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
