#!/usr/bin/env bash
# 一键运行全部测试与大文件演示。所有产物落在 .runtime/（已被 git 忽略）。
set -euo pipefail
cd "$(dirname "$0")"

echo "== 单元/集成测试 =="
python3 -W error::ResourceWarning -m unittest tests.test_bdelta -v

echo
echo "== 大于 64 MiB 大文件演示 =="
python3 -m tests.large_demo --dir .runtime/large --block-size 4096
