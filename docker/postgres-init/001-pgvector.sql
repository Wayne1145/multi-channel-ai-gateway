-- 由数据库初始化阶段安装 pgvector；应用迁移账号无需承担扩展管理职责。
CREATE EXTENSION IF NOT EXISTS vector;