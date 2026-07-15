# data_clean_V0.1.py 清理紀錄

## 程式

- 程式檔名：`data_clean_V0.1.py`
- 輸入資料庫：`seventeen.sqlite3`
- 輸入資料表：`posts`
- 輸出資料庫：`seventeen_clean.sqlite3`
- 輸出資料表：`posts`

## 輸出欄位

| 欄位 | 說明 |
| --- | --- |
| `serial_id` | 新的流水編號，從 1 開始 |
| `num` | 原始 `seventeen.sqlite3` 的 `posts.num` |
| `main_text` | 原始 `seventeen.sqlite3` 的 `posts.main_text` 經清理後的文字 |

## 清理規則

1. 移除 `main_text` 為空白的資料。
   - 包含 `NULL`、空字串、只有空白的文字。

2. 移除重複資料。
   - 以清理後的 `main_text` 判斷是否重複。
   - 若多筆資料的 `main_text` 相同，只保留 `num` 較小、較早出現的那一筆。

3. 清理文字中的儲存雜訊。
   - 統一換行格式：`\r\n`、`\r` 轉為 `\n`。
   - 移除 NULL byte：`\x00`。
   - 移除不可見控制字元，但保留換行 `\n` 與 tab `\t`。
   - 去除文字前後空白。

4. 保留文字內容。
   - 不刪除 emoji。
   - 不刪除 hashtags。
   - 不刪除中文、英文、韓文、日文或其他語言文字。

## 筆數紀錄

| 項目 | 筆數 |
| --- | ---: |
| 輸入資料筆數 | 50,000 |
| 移除空白 `main_text` | 116 |
| 移除重複 `main_text` | 1,046 |
| 輸出資料筆數 | 48,838 |

