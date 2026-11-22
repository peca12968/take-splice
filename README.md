# Range Fetch｜可續傳的產物下載器

發布作業下載到一半斷線時，重新執行相同命令即可接續。工具用 HTTP Range 與強 ETag 識別可重用的開頭，下載完成後核對呼叫端提供的 SHA-256，才把暫存檔移到目標位置。這讓使用舊版產物的程序可以等到新版完整才讀取它。

## 本機重現

需要 .NET 10 SDK。示範與測試另用 Python 3.12 以上的標準函式庫，資料來自本機臨時 HTTP 服務。

```sh
dotnet build src -c Release
python demo.py
python -m unittest -v
```

範例產物有 262,144 位元組。第一個程序收到 65,536 位元組後遇到斷線，既有檔案保留。第二個程序讀取磁碟上的暫存進度，送出 `Range: bytes=65536-` 與 `If-Range: "release-2"`。服務回覆 206，只傳剩下的 196,608 位元組。示範同時核對服務收到的標頭、下載器的統計與最終檔案的每個位元組。[可執行示範](demo.py)

## 下載自己的檔案

將以下三個參數換成來源的直接 HTTP 或 HTTPS 網址、目的檔案路徑，以及發行端提供的 64 位十六進位 SHA-256。工具要求來源回傳未經 HTTP 內容編碼的檔案，並使用傳入的最終網址。

```text
dotnet run --project src -- URL OUTPUT SHA256
```

成功時 stdout 輸出 JSON，其中 `fileBytes` 是成品大小，`reusedBytes` 是沿用的本機位元組數，`downloadedBytes` 是本次成功執行從網路讀入的本文位元組數。目標已符合雜湊時，狀態為 `already_verified`，網路讀取量為零。失敗會傳回非零結束碼及 stderr 診斷。中斷後可再執行同一命令。

## 連線還在，資料卻停止傳輸

來源可能已送出標頭和部分資料，隨後保持連線，讓下一次讀取一直等下去。下載器預設為每次等待本文資料設定 30 秒期限，逾時便結束並釋放目標檔的鎖。可以在原有命令後加上 `--idle-timeout-ms 5000`，把這個期限調成 5 秒。下載可以持續數分鐘，只要每次讀取都能及時收到資料。等待回應標頭由 HttpClient 自身的逾時機制處理。[.NET 串流回應與逾時範圍](https://learn.microsoft.com/en-us/dotnet/api/system.net.http.httpcompletionoption?view=net-10.0#remarks)

以下測試讓伺服器先後在第 65,536 和第 131,072 個位元組停住，兩次都保持連線。前兩個程序各自收到閒置逾時，原先發布的檔案仍可讀。第三個程序使用強 ETag 接續，只下載剩餘的 131,072 位元組，測試逐位元組比對最終檔案。另有案例讓來源每隔 0.1 秒送出一段，整體傳輸超過設定的 1 秒仍成功。

```sh
python -m unittest -v test_download.DownloadTests.test_stalled_full_and_partial_reads_resume_from_last_written_byte
```

## 續傳與版本判定

### 讓來源分段交付

在命令後加上 `--max-range-bytes 65536`，下載器從第一個位元組開始，每次以 HTTP Range 要求最多 65,536 B。首段的強 ETag 會用於後續每次 If-Range 檢查，可和 `--idle-timeout-ms` 一起使用。若來源忽略 Range 並回傳 200，就接收整份檔案。首段沒有強 ETag 或範圍無法滿足時，下載器會改用完整 GET。若 206 的回傳範圍超過已要求的上限，會在寫入該段前結束。

以下示範需要 Node.js 24.2 以上，並使用固定版本的 Chunk Courier 作為真實 HTTP 來源。從本專案目錄執行。

```sh
git clone https://github.com/peca12968/chunk-courier.git ../chunk-courier
git -C ../chunk-courier checkout 945af9569d76f35462584bf2c3ed4c831566e3e0
dotnet build src -c Release
python courier_demo.py ../chunk-courier
```

示範先透過上傳客戶端送出並封存 150,001 B，下載時由代理轉送實際範圍回應。第一個程序取得首段 65,536 B，再取得第二段的 4,096 B 後斷線。先前發布的檔案仍可讀。下一個程序從位置 69,632 接續，只下載剩下的 80,369 B。示範逐筆核對四次 Range 與 If-Range 標頭、最後收據及檔案內容。[跨服務驗證](courier_demo.py) · [上傳來源](https://github.com/peca12968/chunk-courier/tree/945af9569d76f35462584bf2c3ed4c831566e3e0)

### 接續下載時的回應處理

| 來源或檔案狀態 | 工具的處理方式 |
| --- | --- |
| 強 ETag 與已存版本一致 | 驗證 206 的 ETag、起訖、總長及宣告長度後，從已存位置接續。回覆只涵蓋部分剩餘範圍時，再請求下一段 |
| 來源改版或忽略範圍而回覆 200 | 清空舊的暫存內容，從頭讀取這次的完整回覆 |
| 弱 ETag、沒有 ETag，或改用另一個網址或預期雜湊 | 下次使用完整 GET |
| 範圍已失效而回覆 416 | 改送一次完整 GET |
| 區段資訊矛盾或 SHA-256 不符 | 回報失敗並保留原目標。雜湊不符的暫存內容會丟棄 |
| 所有位元組已寫入、尚未發布便中斷 | 若檔頭紀錄了總長度且已有強 ETag，重新核對本機暫存檔的雜湊，成功即可直接發布 |

續傳驗證依據 HTTP 的條件式範圍請求規則。此工具對 206 採嚴格檢查，要求相同的強 ETag 與明確的總長。[RFC 9110 的 If-Range 定義](https://www.rfc-editor.org/rfc/rfc9110.html#section-13.1.5) · [下載流程](src/Download.cs)

## 並行與驗證

工具在目標旁建立專屬的 `OUTPUT.range-fetch/` 目錄，保存暫存資料、版本資訊及鎖檔。同一目標同時只允許一個下載程序，第二個程序會在送 HTTP 請求之前結束。發布使用同一檔案系統的檔案替換，目標內容以整份 SHA-256 驗證。此示範適用於單機的一般本機檔案系統。

十三個黑箱測試會啟動真正的 HTTP 服務，並讓不同 .NET 程序依序下載、重試或競爭同一個目標。測試也刻意讓伺服器停住、送出錯誤範圍或換版內容，核對舊產物、續傳請求與最終交付檔案。[測試](test_download.py) · [GitHub Actions](.github/workflows/ci.yml)

**履歷用語**　以 C# 與 .NET 實作 HTTP 產物續傳工具，結合 Range、強 ETag、SHA-256 與同檔案系統替換，透過跨程序測試驗證斷線接續、版本切換及並行寫入。

本專案使用 AI 輔助開發，所有測試檔案皆為合成資料。
