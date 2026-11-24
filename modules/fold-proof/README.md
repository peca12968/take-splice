# Fold Proof｜雙面小冊 PDF 拼版

把依閱讀順序排列的 PDF 排到橫向紙張的正反面。工具依每帖張數分組，將外層到內層的印面順序寫入新 PDF。可選左翻或右翻、A4 或 Letter、每半頁的最小留白。

**先看範例。** 同一份 11 頁的印刷筆記以兩種順序呈現。閱讀版從封面一路讀到第 11 頁。拼版版的第一面左邊是第 8 頁、右邊是封面，最後一帖有一格刻意留白。[閱讀版](examples/reading.pdf) · [可列印的拼版版](examples/booklet.pdf)

## 印出第一本小冊

在 Python 3.12 環境執行。以下命令會輸出一份 6 個印面的 PDF，需 3 張 A4 紙。

```sh
python -m pip install -r requirements.txt
python fold_proof.py examples/reading.pdf my-booklet.pdf --sheets-per-signature 2
```

列印時選橫向、雙面、短邊翻轉、實際大小，每面印一頁。檔案內含短邊翻轉與原尺寸列印提示，請在印表機驅動程式確認。前兩張依序套疊並對摺成第一帖，再把第三張對摺成第二帖。最後將兩帖依序裝訂。

| 紙張 | 正面左／右 | 背面左／右 |
| --- | --- | --- |
| 第一帖外層 | 8／1 | 2／7 |
| 第一帖內層 | 6／3 | 4／5 |
| 第二帖 | 空白／9 | 10／11 |

`--binding right` 會互換每個印面的左右位置，讓書脊位在右側。`--sheets-per-signature` 預設每帖最多 4 張紙。`--paper letter` 改用美式 Letter，`--margin-mm 6` 指定每格四周最小留白為 6 毫米。命令會以 JSON 列出來源頁數、帖數、紙張數與空白格數。

## 頁面幾何與使用範圍

每頁依可見裁切區與紙張邊界的交集，套用來源旋轉角度、等比例縮放並置中。輸出保留頁面文字與向量圖形。這個工具用於靜態印刷稿，輸出會移除連結互動。含有表單或其他註記的輸入應先匯出成靜態列印版。輸出完成後才替換指定檔案。拼版提供等寬對摺，厚紙多帖的紙厚位移補償須在印前另行處理。[實作](fold_proof.py) · [pypdf 座標變換說明](https://pypdf.readthedocs.io/en/stable/user/cropping-and-transforming.html)

## 如何驗證真的能依序翻閱

```sh
python -m pip install -r requirements-test.txt
python -m unittest -v
```

測試以獨立的 PDFium 引擎重開輸出檔，分別讀出每個印面左右兩格的頁碼，再模擬把外張包住內張。1 到 12 頁、三種每帖張數、兩個裝訂方向共 72 種配置，都應依序讀回原稿頁碼，空白只出現在最後。另以四個不同色塊、偏移裁切區和四種旋轉角度核對渲染像素，確認裁切外的洋紅色沒有印到另一格。[測試](test_fold.py)

本專案使用 AI 輔助開發，範例印刷筆記由專案內的 ReportLab 程式生成。[產生範例](make_sample.py)
