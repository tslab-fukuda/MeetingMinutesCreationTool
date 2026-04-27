# 音声録音

このワークスペースには、Windows 上で会議を録音し、文字起こしを確認するための簡単なツールが入っています。

## 録音

入力デバイス一覧を表示する:

```powershell
$env:PYTHONIOENCODING='utf-8'
python .\tools\record_audio.py --list-devices
```

`Ctrl+C` を押すまで録音する:

```powershell
python .\tools\record_audio.py
```

10秒だけ録音する:

```powershell
python .\tools\record_audio.py --seconds 10
```

特定の入力デバイスを使う:

```powershell
python .\tools\record_audio.py --device 1 --seconds 10
```

保存先を指定して録音する:

```powershell
python .\tools\record_audio.py --output .\recordings\meeting_test.wav --seconds 10
```

`--output` を省略した場合は、次のようなタイムスタンプ付きファイル名で `recordings/` に保存されます。

```text
recordings/meeting_recording_20260425_153000.wav
```

## API 文字起こし

OpenAI Audio API を使って WAV ファイルを文字起こしする:

```powershell
python .\tools\transcribe_audio.py .\recordings\meeting_recording_20260425_153915.wav
```

より高品質なモデルを使う:

```powershell
python .\tools\transcribe_audio.py .\recordings\meeting_recording_20260425_153915.wav --model gpt-4o-transcribe
```

固有名詞や会議の文脈を補足する:

```powershell
python .\tools\transcribe_audio.py .\recordings\meeting_recording_20260425_153915.wav --prompt "大学の会議。固有名詞を正確に書く。"
```

### APIキー

文字起こしツールは、まず環境変数 `OPENAI_API_KEY` を参照します。  
設定されていない場合は、このファイル内に直接書かれた `sk-...` 形式のキーも読み取れます。

推奨:

```powershell
$env:OPENAI_API_KEY="your_api_key_here"
```

このワークスペースでは次の形式も一応サポートしていますが、安全性は低いです:

```text
sk-...
```

## ライブ監視

録音と文字起こしを同時に行う:

```powershell
python .\tools\monitor_meeting.py
```

ローカル文字起こしで監視する:

```powershell
python .\tools\monitor_meeting.py --transcriber local --language ja
```

課金設定後に OpenAI API で監視する:

```powershell
python .\tools\monitor_meeting.py --transcriber api --language ja
```

よく使うオプション例:

```powershell
python .\tools\monitor_meeting.py --device 1 --segment-seconds 15 --status-seconds 3
```

短時間のテスト実行:

```powershell
python .\tools\monitor_meeting.py --seconds 10 --segment-seconds 5 --language ja
```

このツールには次の利点があります。

- 音声は録音終了時にまとめて保存するのではなく、録音中に継続して保存されます。
- 文字起こし結果は区間ごとに生成されるので、早い段階で異常に気づけます。
- 入力音量が低い場合に警告が出るため、マイク未接続や入力ミスの検知に役立ちます。
