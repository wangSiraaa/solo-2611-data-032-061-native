# 数据工程独立任务初始工作区

本仓库用于独立的数据工程任务。本次需求以当前会话提供的完整题面为准；初始快照没有业务实现、测试答案或业务数据。

## 环境

- Linux、Python 3.11，提供 pip 和 venv。
- PostgreSQL 16 的 `initdb`、`pg_ctl`、`psql`、`createdb` 已加入 PATH。
- 当前为普通用户，可在自己拥有的目录初始化独立数据库。
- Python 标准库包含 SQLite；根据题目选择实际需要的技术。

原生环境初始化示例见 [environment/README.md](environment/README.md)。请将依赖、数据库、运行日志及临时验证数据放在忽略目录中，并为最终实现提供清楚的运行说明。

## 本次实现：bdelta 离线差量工具

纯 Python 3.11 标准库实现的离线 COPY/ADD 差量工具，设计说明、补丁格式、
测试覆盖与实测指标见 [README.bdelta.md](README.bdelta.md)。

```bash
./run_all.sh                      # 全部 28 项测试 + 70 MiB 大文件演示
python -m bdelta generate corpus --dir .runtime/demo --block-size 1024
```
