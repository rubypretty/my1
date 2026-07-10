# Threads Selenium Scraper

用 Selenium 抓取 Threads 貼文頁面的公開文字資料，並輸出成 Excel。

## 安裝

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 使用方式

把要抓取的 Threads URL 放在 `url_table.xlsx`，第一列使用 `url` 欄位，然後執行：

```powershell
python search_threads_today.py
```

預設會輸出 `threads_posts.xlsx`，包含：

- `author`
- `post_time`
- `view_count`
- `main_text`
- `愛心`
- `留言`
- `轉發`
- `分享`
- `related_or_replies`

`post_time` 會轉成 `yyyy.mm.dd hh:mm` 格式，例如 `2026.07.10 17:22`。

## 搜尋今日貼文

```powershell
python search_threads_today.py
```

## 注意事項

- `.env`、Chrome profile、Excel/JSON 輸入輸出檔都不會提交到 Git。
- 請遵守 Threads 的服務條款、robots/平台規範與當地法律。
