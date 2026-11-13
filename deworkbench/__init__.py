"""deworkbench — reproducible bulk count-matrix differential-expression workbench.

The pipeline is intentionally split into explicit stages:

  plan  -> load/validate -> precheck (identifiability) -> QC (no auto-removal)
        -> frozen low-expression filter -> NB-GLM fit -> contrasts -> report

Everything written under a run directory is self-contained so an interrupted or
repeated run reproduces the same numbers without notebook state.
"""

__version__ = "1.0.0"
