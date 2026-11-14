"""Offline comparison of two PostgreSQL EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) plans.

The package never connects to a database, never recommends indexes and
never rewrites SQL.  It only reads plan files and an optional capture
manifest, then produces a machine-readable correspondence table and a
short Markdown report.
"""

__version__ = "0.1.0"
