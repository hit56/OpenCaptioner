# 内容审核词表（离线文件，不入库）

将 `political.dict.example` / `profanity.dict.example` 复制为同名 `.dict` 后按需填写，每行一个关键词。

```bash
cp dict/political.dict.example dict/political.dict
cp dict/profanity.dict.example dict/profanity.dict
```

`*.dict` 已加入 `.gitignore`。未放置词表或 `ENABLE_CONTENT_SAFETY=0`（默认）时，审核关键词匹配为空。
