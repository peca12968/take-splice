# Sprite Crate｜透明動畫幀圖集打包器

一個包裹在 64 × 64 的畫布裡移動，最後一幀消失。如果每幀都直接裁到包裹大小，播放時包裹就會停在同一處。Sprite Crate 將可見的部分排入共用圖集，也記錄每幀原來的畫布大小與偏移量，讓播放端能把包裹放回正確位置。

![從圖集重建的九幀包裹動畫，末幀為空白](examples/parcel-replay.png)

## 重現打包與播放

打包器使用 Go 1.25 與標準函式庫。示範播放端使用 Python 3.12 與 Pillow，從 ZIP 讀取清單和圖集，逐幀重建並與輸入像素比對。

```sh
go build -o sprite-crate .
python -m pip install Pillow==13.0.0
python demo.py ./sprite-crate parcel-demo
python -m unittest -v
```

Windows 可使用 `go build -o sprite-crate.exe .` 與 `python demo.py ./sprite-crate.exe parcel-demo`。示範需要新的輸出目錄，會留下九張來源 PNG、圖集封裝 `parcel.zip` 與重建圖 `replayed.png`。

| 素材 | 數量與尺寸 | 畫布像素總數 |
| --- | --- | ---: |
| 合成動畫來源 | 9 幀，各 64 × 64 | 36,864 |
| 圖集 | 1 頁，48 × 48，含外緣 | 2,304 |

這組素材的圖集像素數為原畫布總數的 1/16。空白幀保留播放位置與原尺寸，清單以頁碼 −1 表示。數字按解碼後的畫布面積計算。[實際圖集](examples/parcel-atlas.png) · [播放與重建程式](demo.py)

## 打包自己的 PNG

```sh
./sprite-crate -size 256 -out icons.zip idle.png selected.png pressed.png
```

每張圖使用唯一檔名。ZIP 裡有 `manifest.json` 及零張以上的正方形 PNG。清單的 `page` 是從零開始的頁次。`atlas` 依序記錄左上角 x、y、寬與高，像素範圍採左閉右開。`offset` 是裁切內容在原畫布中的左上角位置。播放時建立 `source_size` 大小的透明畫布，從指定頁取出矩形，原樣貼至 `offset`。

工具使用 8 位元 RGBA，16 位元來源取各通道的高八位。全透明處的 RGB 歸零。每個圖塊四周都複製一個像素的邊緣，讓雙線性取樣靠近邊界時仍可讀到本幀顏色。先依高度、寬度排序，同尺寸依檔名排序，再逐列排版。此策略能重現同一份輸出，實際頁數會隨素材形狀改變。[打包實作](atlas.go)

可設定 3 至 4,096 像素的頁邊長。每張來源畫布與整批裁切內容各有 16,777,216 像素上限。輸出先寫入同目錄暫存檔，成功關閉 ZIP 後才替換指定檔案。

黑箱測試以 64 張固定種子的圖樣逐像素核對裁切範圍、半透明顏色、外緣與跨頁位置，並將輸入順序顛倒後比對整份 ZIP。另有色盤 PNG、16 位元低透明度、空白幀，以及失敗時保留既有輸出的案例。[測試](test_atlas.py)

本專案使用 AI 輔助開發，示範圖形由程式產生。
