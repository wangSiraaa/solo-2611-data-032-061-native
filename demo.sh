#!/usr/bin/env bash
# 一键演示：生成合成配置 -> 全局穷举优化（含用户顺序、SQLite 归档）
#           -> 人类可读解释 -> 独立验证（含洗牌不变性）-> 八表计时
# 纯本地运行，不访问任何外部数据库或网络。
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p var
DB="var/demo_archive.sqlite3"
rm -f "$DB"

echo "################ 1. 生成合成配置（可手算贪心反例） ################"
python3 -m planopt generate --preset greedy_trap_3 -o var/greedy_trap_3.json
cat var/greedy_trap_3.json

echo
echo "################ 2. 全局穷举 + 用户顺序 + SQLite 归档 ################"
python3 -m planopt optimize -i var/greedy_trap_3.json \
    --user-order S L M --db "$DB" --preset-tag greedy_trap_3 \
    -o var/greedy_trap_3.result.json

echo
echo "################ 3. 人类可读解释 ################"
python3 -m planopt explain -i var/greedy_trap_3.json \
    --result var/greedy_trap_3.result.json

echo
echo "################ 4. 独立验证（穷举/DP/逐前缀重算/洗牌） ################"
python3 -m planopt verify -i var/greedy_trap_3.json \
    --result var/greedy_trap_3.result.json --shuffle

echo
echo "################ 5. 三角形并列场景（码点序裁决） ################"
python3 -m planopt generate --preset triangle_tie_3 | python3 -m planopt optimize

echo
echo "################ 6. 八表上限计时（40320 个排列） ################"
python3 -m planopt generate --preset eight_8 -o var/eight_8.json
python3 -m planopt optimize -i var/eight_8.json --db "$DB" --preset-tag eight_8

echo
echo "################ 7. 从 SQLite 归档复核 run_id=1 ################"
python3 -m planopt verify --run-id 1 --db "$DB"

echo
echo "演示完成，产物位于 var/（SQLite: $DB）"
