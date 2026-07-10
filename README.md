# Threads Selenium Scraper

一個簡單的 Threads 抓文範例，使用 Selenium 開啟瀏覽器、手動登入，然後從指定頁面收集貼文文字與連結。

## 安裝

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 使用

```powershell
python threads_scraper.py "https://www.threads.com/@username" --max-posts 20 --output threads_posts.json
```

輸出 CSV：

```powershell
python threads_scraper.py "https://www.threads.com/@username" --max-posts 20 --output threads_posts.csv
```

第一次使用時建議不要加 `--headless`，瀏覽器開啟後可以手動登入 Threads。

## 使用已登入 Chrome

如果 Selenium 開出的瀏覽器一直觸發登入驗證，請改用專用 Chrome：

```powershell
.\start_debug_chrome.ps1
```

在開出的 Chrome 完成 Threads 登入後，不要關掉 Chrome，另外執行：

```powershell
python threads_scraper.py
```

## 注意

- 請遵守 Threads 的服務條款、robots/平台規範與當地法律。
- 不要抓取非公開、敏感或未經授權的個人資料。
- 建議降低頻率，避免大量請求造成帳號或 IP 被限制。
