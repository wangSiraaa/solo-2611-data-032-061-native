"""Zonal-statistics query tool for single-band rasters with rotated grids."""

from .core import load_config, query_region, run_queries

__all__ = ["load_config", "query_region", "run_queries"]
__version__ = "0.1.0"
