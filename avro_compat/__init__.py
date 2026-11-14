"""Avro writer->reader 兼容性预检与投影（受限 Avro 子集）。

公开入口：
- :func:`avro_compat.schema.validate_schema` 校验单个 schema 是否落在支持子集内
- :func:`avro_compat.resolver.build_plan` 建立 writer -> reader 解析计划
- :func:`avro_compat.projection.project_records` 按计划投影已解码记录
- :func:`avro_compat.cli.main` 命令行入口（check / project）
"""

from .schema import SchemaError, validate_schema, load_json
from .resolver import CompatibilityError, build_plan, CompatibilityReport
from .projection import ProjectionError, project_records

__all__ = [
    "SchemaError",
    "CompatibilityError",
    "CompatibilityReport",
    "ProjectionError",
    "validate_schema",
    "load_json",
    "build_plan",
    "project_records",
]
