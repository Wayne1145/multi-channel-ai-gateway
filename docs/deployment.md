# 部署文档

## 资源建议

MVP 至少需要 2 核、2GB 内存和 5GB 可用磁盘。若启用文件知识库，应扩容磁盘并增加对象存储。

## 1Panel

项目可放置于：

```text
/opt/1panel/docker/compose/wecom-ai-gateway
```

在 1Panel 的「容器 → 编排」中导入 `docker-compose.yml`，网站反向代理到：

```text
http://127.0.0.1:18082
```

## 备份

至少备份：

- `data/postgres/`
- `.env`（单独加密备份）

推荐使用 `pg_dump` 做逻辑备份，不要在数据库运行时直接复制数据目录。

## 升级

```bash
git pull --ff-only
docker compose build
docker compose up -d
```

API 容器启动时会先执行 `alembic upgrade head`。禁止通过删除数据目录解决升级问题。
