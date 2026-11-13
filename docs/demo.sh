#!/usr/bin/env bash
# 完整本地演示：不依赖网络、不连业务库，仅用 Python 标准库的内存 SQLite。
# 用法: bash docs/demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "############ 1) generate：AST 与受控 SQLite 表达式（不执行） ############"
python3 -m sql3v generate examples/not_in_null.json

echo
echo "############ 2) compare：NOT IN 含 NULL，筛选等价但值不等价 ############"
python3 -m sql3v compare examples/not_in_null.json

echo
echo "############ 3) compare --sqlite：逐赋值与内存 SQLite 交叉核对 ############"
python3 -m sql3v compare examples/different_counterexamples.json --sqlite

echo
echo "############ 4) explain：单赋值逐步三值求值 ############"
python3 -m sql3v explain examples/different_counterexamples.json --assign a=2,b=NULL

echo
echo "############ 5) verify：全量交叉核对 + 手工 AND/OR/NOT 真值表 ############"
python3 -m sql3v verify examples/demorgan.json

echo
echo "############ 6) 非法输入定位（预期退出码 2） ############"
for f in examples/invalid_arithmetic.json examples/invalid_subquery.json \
         examples/invalid_missing_operand.json examples/invalid_duplicate_value.json; do
  echo "--- $f"
  python3 -m sql3v compare "$f" || echo "（已按预期拒绝，退出码 $?）"
done

echo
echo "############ 7) 标准输入声明 + JSON 输出 ############"
cat examples/double_negation.json | python3 -m sql3v compare - --json | python3 -m json.tool | head -20
