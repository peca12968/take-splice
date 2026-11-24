# 媒體處理與檔案交付

從音訊、字幕、圖集及印刷檔的產製，接續大檔傳輸與完整性恢復。[工具入口與各模組的重跑方式](modules/README.md)。

剪輯清單要把第一段錄音的後半段接上另一段錄音，再重播第一段的一小節。Take Splice 用 C# 依序從來源 WAV 複製指定範圍的 PCM 資料，產生新 WAV，以及每一段在輸出檔的起始幀。立體聲的一幀包含同一時刻的左右聲道，兩個聲道一起剪接。

## 產生可播放的示範

需要 .NET 10 SDK 與 Python 3.12 或 3.13。在專案根目錄執行，最後一行會建立新的 `listen` 目錄，內含兩份合成音、剪輯清單與可播放的 `edit.wav`。省略目錄參數會改在暫存目錄內完成驗證。

```sh
dotnet build src -c Release
python -m unittest -v
python demo.py listen
```

| 來源 | 起始幀（含） | 結束幀（不含） | 輸出起始幀 | 長度（幀） |
| --- | ---: | ---: | ---: | ---: |
| first.wav | 24,000 | 48,000 | 0 | 24,000 |
| second.wav | 99 | 24,100 | 24,000 | 24,001 |
| first.wav | 24,000 | 36,000 | 48,001 | 12,000 |

兩份來源都是 48 kHz、16 位元立體聲。第三段重用第一段的一部分，第二段的長度刻意多一幀。輸出共有 **60,001 幀、240,004 個 PCM 位元組**。示範以 Python 標準函式庫重新讀取 WAV，逐位元組比對獨立切出的三段資料，並核對隨專案提供的試聽檔。[試聽 WAV](examples/preview.wav) · [示範程式](demo.py)

## 使用自己的錄音

建立 UTF-8 TSV 清單，第一列依序為 `source`、`start_frame`、`end_frame`，欄位間用一個 Tab 分隔。檔名可含空格，相對路徑從清單所在目錄解析。幀從零起算，區間須非空且位於來源範圍內。每列依順序輸出，相同來源可重複出現，區間也可以重疊。

```sh
dotnet run --project src -c Release -- listen/edit.tsv another-edit.wav
```

成功會回傳結束碼 0，並在標準輸出寫入 JSON，列出格式、幀數、來源區間與輸出起始幀。命令需要一個尚未存在的輸出檔名。若有來源缺失、格式不一致或區間越界，會以結束碼 2 回報原因。

### 降低接點的波形突變

```sh
dotnet run --project src -c Release -- listen/edit.tsv smooth-edit.wav --edge-fade-frames 240
```

在 48 kHz 音訊中，240 幀相當於 5 毫秒。每個內部接點前線性淡出、後線性淡入。兩側增益若在短片段中重疊，就採較小值。各聲道使用相同增益，第一段的起頭與最後一段的收尾維持原樣。省略參數或填 0 時沿用原本的硬剪接。

這個模式保留總幀數、片段偏移及報表欄位。整合測試用新音訊重新輸出 WebVTT，核對六句字幕及時碼與原範例一致，並從實際 WAV 讀取兩個接點前後的取樣值。[作品站共用的接點數值](examples/edge-fade-demo.json) · [實際 WAV 與字幕驗證](test_splice.py)

## 打包與驗收媒體交付

先依上方步驟建置剪輯器並產生 `listen`，再把 WAV 與對時後的字幕放進一個 ZIP。同一批輸入與淡化參數可重建位元組相同的交付檔。

```sh
python modules/cue-conform/deliver.py build listen/edit.tsv modules/cue-conform/examples/tracks.json delivery.zip --edge-fade-frames 240
python modules/cue-conform/deliver.py verify delivery.zip
```

`manifest.json` 記下 WAV 的取樣率、聲道、位元深度與總幀數，以及字幕句數和最晚結束毫秒。驗收會核對兩個檔案的位元組數與 SHA-256，直接從 ZIP 讀取 PCM 和 WebVTT，並檢查字幕結束時間是否落在音訊向上取整的毫秒邊界內。ZIP 內使用固定檔名，匯出驗證成功後才發布成品。同名匯出發生競爭時只會成功一次。

收件者可從發送方另取 `build` 回傳的 ZIP `sha256`，將 64 位十六進位字串傳給 `verify --sha256`，核對收到的完整版本。範例將兩次出現的「高音」改為「旁白」，ZIP 仍可開啟，但驗收會指出字幕的 SHA-256 不符。[CLI](modules/cue-conform/deliver.py) · [範例驗收資料](examples/delivery-demo.json) · [端對端測試](test_delivery.py)

已歸檔的錄音可用 Shard Rescue 只取回指定的位元組區間。範例跨兩條受損的條帶還原 32,768 B，並與原檔逐位元組比對。第三條帶超過修復能力，完整還原仍會失敗。[片段還原與重跑](modules/shard-rescue/README.md#只還原指定的位元組區間)

## 格式與檔案處理

- 接受小端 RIFF/WAVE 中的整數 PCM，支援單聲道、立體聲與 16、24 位元。所有片段需有相同取樣率、位元深度與聲道數。預設為硬剪接，可依音訊內容選擇在接點加上短淡出及淡入。
- 讀取 RIFF chunk 的宣告長度與偶數位元組對齊，容許額外中繼資料及不同的 chunk 順序。逐一檢查資料邊界、取樣框大小與位元組率，輸出重建 `fmt` 和 `data` 兩個 chunk。RF64、浮點 PCM、壓縮音訊與多聲道配置超出這個版本的支援範圍。[RIFF 格式](https://learn.microsoft.com/en-us/windows/win32/xaudio2/resource-interchange-file-format--riff-) · [WAV 解析與標頭](src/Wave.cs)
- 開始複製前驗證整份清單和 RIFF 可表示的輸出大小。複製使用 64 KiB 緩衝區，維持每個不同來源的檔案連線，工作期間來源應保持穩定。完成後將同目錄暫存檔移到輸出名稱。測試會讓兩個程序競爭相同名稱，確認只成功發布一次。[剪接實作](src/Program.cs)

## 驗證

以固定種子在四種聲道與位元深度組合下產生 32 份清單。每次都透過 CLI 實際產檔，再以 Python `wave` 讀回，核對格式、全部取樣資料、逐段偏移，以及 24 位元單聲道的奇數長度補齊。另測試截斷、重複資料區塊、後段格式不符、輸出超過 RIFF 上限及檔名競爭。[黑箱測試](test_splice.py)

**履歷用語**　以 C# 實作取樣幀精度的多來源 WAV 剪輯器，支援 16/24 位元 PCM，以固定大小緩衝區複製音訊，並用 Python 逐位元組驗證剪接邊界與聲道排列。

本專案使用 AI 輔助開發。示範音訊由程式產生。

[同庫模組與整合測試](modules/README.md)
