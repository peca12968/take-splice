# Chunk Courier｜回覆遺失後繼續上傳檔案

客戶端送出第一段，服務已經寫入資料庫，確認訊息卻在途中遺失。Chunk Courier 會先查詢已提交的位置，再從下一個位元組繼續。範例檔案共 1,048,649 B，第一段為 1 MiB，確認遺失後只需再傳 73 B。

## 本機操作

使用 Node.js 24.2 以上，從專案目錄執行 `node server.mjs`。服務預設監聽本機 4188 埠，將進度與資料保存在 `.courier/uploads.sqlite`。可用 `PORT` 與 `COURIER_DB` 指定其他埠與資料庫。

在另一個終端機上傳本專案的 README，下載後可與原檔比對。

```sh
node upload.mjs http://127.0.0.1:4188 handbook README.md
curl --fail http://127.0.0.1:4188/uploads/handbook/content -o handbook-copy.md
node --test
```

重跑相同上傳命令時，已封存的檔案會直接取得原收據。若前次停在中途，客戶端會讀取伺服器進度。輸出中的 `resumedAt` 是本次啟動時已提交的長度，`reconciliations` 是分段回覆不明或位置衝突後查詢進度的次數。上傳期間請讓來源檔案內容保持穩定。

## 哪些操作可以重送

| 操作 | 語意 |
| --- | --- |
| `PUT /uploads/{id}` | 提交 JSON 清單，含長度 `size` 與整檔 `sha256`。相同 ID 與清單可取回目前狀態 |
| `PATCH /uploads/{id}` | 提交最多 1 MiB 的二進位內容，以 `Upload-Offset` 指定起點，`Chunk-SHA256` 指定該段雜湊 |
| `GET /uploads/{id}` | 查詢已提交的連續位元組數及封存狀態 |
| `POST /uploads/{id}/seal` | 長度到齊後依序雜湊各段，整檔一致才封存。空檔案也可封存 |
| `GET /uploads/{id}/content` | 下載已封存的內容。支援單一 HTTP 位元組範圍及強 ETag 的 If-Range |

客戶端先順序讀取一次本機檔案計算清單，再依伺服器確認位置讀取待送區段。SQLite 在同一筆寫入交易中插入分段並前推位置。相同起點、長度與雜湊可重送取得 200，首次寫入為 201。缺口或與既有區段衝突回傳 409。單段或整檔雜湊不符回傳 422。已提交內容為不可變資料，若最終雜湊不符，可用正確檔案和新的上傳 ID 重新建立。封存後可按需串流讀取跨區段範圍。[儲存交易](upload-store.mjs) · [HTTP Range 語意](https://www.rfc-editor.org/rfc/rfc9110.html#section-14.2)

## 故障演練

- 兩個獨立的 HTTP 服務程序共用同一資料庫。12 個請求同時提交同一段，只有一筆新寫入，另外 11 筆取得重送收據。停止全部程序後再啟動，從原位置接續並逐位元組核對下載內容。
- 測試代理收到第一段已寫入的 HTTP 201 後切斷客戶端連線。實際送出的兩個 PATCH 起點為 0 與 1,048,576。重新執行已完成的上傳，PATCH 次數維持兩次。
- 讀取跨分段區間、開放尾端與尾碼範圍時逐位元組比對。過期的 If-Range 取回整檔，超出範圍則回報 416 和完整長度。[HTTP 整合測試](http.test.mjs) · [交易測試](upload-store.test.mjs)

這是使用本機回送介面的單機作品集示範。封存時的整檔雜湊由服務程序同步計算，大檔案會占用該程序的事件迴圈。服務以 SQLite 保存完整資料，持續使用時需為已上傳檔案預留磁碟空間。本專案使用 AI 輔助開發，測試檔案均為合成資料。

[同庫模組與整合測試](modules/README.md)
