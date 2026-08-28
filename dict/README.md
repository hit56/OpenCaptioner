# Content-safety word lists

**English** | [中文](#中文)

Offline keyword files are **not** stored in git.

Copy the examples to `.dict` files and put one keyword per line:

```bash
cp dict/political.dict.example dict/political.dict
cp dict/profanity.dict.example dict/profanity.dict
```

`*.dict` is gitignored. Matching is empty if the files are missing or `ENABLE_CONTENT_SAFETY=0` (default).

---

## 中文

内容审核词表为离线文件，不入库。将 `political.dict.example` / `profanity.dict.example` 复制为同名 `.dict` 后按需填写，每行一个关键词。未放置词表或 `ENABLE_CONTENT_SAFETY=0`（默认）时，审核关键词匹配为空。
