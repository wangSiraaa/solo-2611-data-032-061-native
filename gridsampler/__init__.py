"""Local gridded temperature sampler.

Reads two classic NetCDF grid files by dimension name, performs four-corner
bilinear sampling at station coordinates, and merges results by timestamp.
"""

__version__ = "1.0.0"
