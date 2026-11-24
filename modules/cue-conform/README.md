# Cue Conform｜剪輯後的字幕重排

一段錄音被剪掉片頭、插入另一段錄音，再重播原來的一小節。Cue Conform 讀取 Take Splice 實際輸出的剪輯紀錄，依取樣幀位置裁切、平移或重複來源字幕，輸出可搭配剪輯版音訊播放的 WebVTT 字幕檔。

## 重現三段剪輯

需要 Python 3.12，執行工具只用標準函式庫。

```sh
python conform.py examples/edit.json examples/tracks.json edited.vtt
python -m unittest -v
```

示範使用每秒 48,000 幀的兩份合成音訊。三個片段共有 60,001 幀，其中一個接點落在兩個整數毫秒之間。五條來源字幕重排為六條，原來的「片頭」被剪掉，「高音」與「延音」在第三段再次出現。兩句原本同時可見的字幕仍可重疊。[剪輯後的音訊](https://github.com/peca12968/take-splice/blob/9774ce1ed2a2fd7db3d9efa07931b9fb2f6911f1/examples/preview.wav) · [完整輸出字幕](examples/edited.vtt)

| 字幕 | 剪輯後出現的時間，單位為毫秒 |
| --- | --- |
| 高音 | 0–220、1,001–1,221 |
| 延音 | 200–450、1,201–1,251 |
| 低音 | 500–748 |
| 節拍 | 798–1,001 |

### 兩句同時出現時，保留哪一句放在畫面上方

在第 210 毫秒，「高音」和「延音」同時出現。「高音」設定在畫面高度的 15%，字幕框寬 38%，文字靠起點對齊。「延音」設定在高度的 80%，框寬 32%，文字靠終點對齊。到了第 1,210 毫秒，重播片段再度出現這兩句，兩組定位與對齊設定都跟著各自的字幕。[來源字幕](examples/first.vtt) · [剪輯後字幕](examples/edited.vtt)

可用本機 Chrome 核對播放器實際讀到的設定。將下列命令中的 `google-chrome` 換成本機 Chrome 的執行檔路徑。檢查程式會執行字幕 CLI，將輸出交給瀏覽器原生文字軌解析，再比對兩個時間點涵蓋的字幕 ID、文字、行位置、水平位置、寬度與對齊。[瀏覽器驗證](browser_check.py)

```sh
python browser_check.py google-chrome
```

## 使用自己的音訊與字幕

第一個參數是 Take Splice 成功剪輯後寫到標準輸出的 JSON，包含取樣率、來源幀區間與每段在輸出中的起始幀。第二個參數是字幕對照檔，鍵使用剪輯紀錄的來源名稱，值是相對於對照檔的字幕路徑。無字幕的來源可明確設為 null。第三個參數需使用新的輸出檔名；工具在輸出目錄透過硬連結發布完成的檔案。[剪輯紀錄](examples/edit.json) · [對照檔](examples/tracks.json)

來源支援 UTF-8 WebVTT 的純文字字幕、選用的 cue ID、多行文字與 NOTE 註解。輸出 ID 依片段與來源 cue 編號產生，方便追查同一句字幕的多次出現。每條字幕可使用 `line`、`position`、`size`、`align` 與 `vertical`。例如 `line:-1,end` 採用負行號，`position:80%,center` 以字幕框中心定位。遇到重複設定、無效值或百分比超過 100%，CLI 會指出輸入檔與字幕區塊。含時間標籤的逐字字幕、REGION 與 STYLE 區塊仍超出支援範圍。[WebVTT 字幕設定規格](https://www.w3.org/TR/webvtt1/#cue-settings)

## 如何處理跨幀邊界

### 先校正聲道之間的時間差

如果字幕是聽參考聲道打的，而成品使用另一條有固定延遲的聲道，可以在對照檔為該來源指定字幕與 Channel Lag 報告。兩個路徑均相對於對照檔。以參考聲道為 1、成品聲道為 2 的量測為例：

```json
{"stereo.wav": {"file": "source.vtt", "alignment": "measured.json"}}
```

先用 Channel Lag 的 `--channel 1 --compare-channel 2` 產生 JSON 報告。程式核對兩個聲道的定位狀態、取樣率與幀索引，使用帶正負號的幀差平移來源字幕，再按每一刀的幀區間裁切。無法判定唯一延遲時會終止匯出。這項對時適用於固定延遲。[量測工具](https://github.com/peca12968/take-splice/tree/main/modules/channel-lag)

整合範例使用同一份 48 kHz 立體聲錄音，先量到聲道 2 晚 417 幀，再用 Take Splice 剪出兩段。原字幕 1.000–1.100 秒，在第一段落到 9–109 毫秒，在重複使用的片段落到 300–326 毫秒。交換聲道後得到 −417 幀，兩段分別顯示於 0–92 與 300–308 毫秒。可安裝 Channel Lag 的 requirements.txt 並以 Release 組態建置 Take Splice，再從本專案執行 `python alignment_demo.py ../channel-lag ../take-splice`。[跨專案驗證](alignment_demo.py)

### 裁切與毫秒刻度

時間區間含起點、排除終點。工具先在整數尺度上算出來源字幕與所選幀區間的交集，平移到輸出後才把兩端都向上取到整數毫秒。這使每一個整數毫秒所顯示的字幕與原音訊對得上。若一段交集完全落在兩個毫秒刻度之間，會在 `sub_ms_omitted` 計數中列入一次。WebVTT 的時間解析度為毫秒，因此最後一條字幕的結束時間可能比音訊結束多出不到 1 毫秒。[實作](conform.py)

測試以 120 份合成剪輯紀錄，逐毫秒從輸出音訊的位置反查來源，使用有理數運算核對當時應顯示的所有字幕。跨專案測試另外執行固定版本的 C# 剪輯器，實際產生 WAV 與紀錄，再執行字幕 CLI 比對輸出檔。Windows 與 Linux 執行相同流程。[逐毫秒測試](test_conform.py) · [跨專案驗證](integration.py)

本專案使用 AI 輔助開發。範例字幕描述程式產生的合成音。
