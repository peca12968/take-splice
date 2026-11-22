# Channel Lag｜量測雙聲道錄音延遲

同一個同步標記經過兩條錄音路徑後，其中一軌可能比較晚、比較大聲，甚至極性相反。這個工具從 WAV 檔的指定聲道找出標記的起始取樣幀，並能量出兩軌的時間差。

## 重跑兩秒錄音

在 Python 3.12 環境執行下列命令。示範程式將三個合成 WAV 寫入新的目錄。

```sh
python -m pip install -r requirements.txt
python demo.py demo-audio
python locate.py demo-audio/marker.wav demo-audio/stereo.wav --channel 1 --compare-channel 2
```

48 kHz 錄音裡，第一軌的標記從幀索引 65,545 開始。第二軌的標記經過反相、放大與直流位移，另混有雜訊，從 65,962 開始。輸出 `delay_frames` 為 **417**，即第二軌晚 **8.6875 毫秒**。交換聲道次序後，延遲為 −417 幀。幀索引從 0 起算。

將輸入換成 `demo-audio/echo.wav`，第二軌在 81,000 幀又出現一次標記。工具會列出兩個候選位置，將該聲道判為 `ambiguous`，延遲輸出為 `null`。

## 輸入與判讀

標記須為單聲道，錄音可為多聲道，兩者皆使用同取樣率的 16 位元未壓縮 PCM WAV。聲道編號從 1 起算。省略 `--compare-channel` 即可只定位單一聲道。分數保留正負號，負值代表標記極性相反。

工具分塊讀取錄音，保留上一塊末端以涵蓋橫跨邊界的標記。每個完整的候選視窗都利用 FFT 互相關和滑動平方和計算 Pearson 相關係數，再按絕對值保留前兩名。無變化的視窗無法計算相關係數。第一名至少須達到 `--minimum`（預設 0.8），而且領先第二名超過 `--gap`（預設 0.05）。相鄰但無法區分的起始幀也會判為歧義。這適合尋找單一已知標記；跨裝置的時鐘漂移需多個時間點才能估計。[SciPy 互相關文件](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.correlate.html)

成功定位的結束碼為 0，標記缺失或有歧義為 1，檔案或參數錯誤為 2。執行 `python -m unittest -v` 可逐視窗對照獨立計算、測試讀取邊界、反相、雜訊、回音及命令列狀態。範例資料為固定種子產生，專案以 AI 輔助開發。
