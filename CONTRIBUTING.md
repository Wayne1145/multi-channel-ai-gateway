# Contributing

欢迎提交 Issue 和 Pull Request。提交前请运行：

```bash
uv sync --extra dev
uv run ruff check src tests migrations
uv run pytest
```

代码注释和文档可使用简体中文；公共 API 名称保持英文。任何测试夹具都必须使用合成身份与虚构凭据。
