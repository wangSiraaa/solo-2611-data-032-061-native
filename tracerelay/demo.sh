#!/usr/bin/env bash
# 一键端到端演示：生成 -> 导入 -> 分析 -> 核验 -> 报告。全部本地合成，不采集真实系统。
set -euo pipefail
cd "$(dirname "$0")"
DB="${DB:-var/demo.db}"
rm -f "$DB" "$DB-wal" "$DB-shm"
echo "== 1. 生成 11 个合成微案例 =="
python3 -m tracerelay.cli generate --what cases --data-dir var/data
echo "== 2. 导入 SQLite =="
python3 -m tracerelay.cli --db "$DB" import var/data/case_*.jsonl
echo "== 3. 分析两套账本 =="
python3 -m tracerelay.cli --db "$DB" analyze
echo "== 4. 独立核验守恒/不重计 =="
python3 -m tracerelay.cli --db "$DB" verify
echo "== 5. 批量报告（前 18 行） =="
python3 -m tracerelay.cli --db "$DB" report | head -18
echo
echo "完成。查看界面：python3 -m tracerelay.cli --db $DB serve"
