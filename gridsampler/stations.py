"""Station CSV loading and validation."""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

from .errors import SamplerError


@dataclass(frozen=True)
class Station:
    station_id: str
    lat: float
    lon: float


def load_stations(path: str) -> tuple[list[Station], list[str]]:
    csv_path = Path(path)
    if not csv_path.is_file():
        raise SamplerError(
            f"stations file not found: {csv_path}", path=str(csv_path),
            kind="file_error",
        )
    try:
        with csv_path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                raise SamplerError(
                    "stations CSV has no header", path=str(csv_path),
                    kind="invalid_stations",
                )
            required = {"station_id", "lat", "lon"}
            missing = required - set(reader.fieldnames)
            if missing:
                raise SamplerError(
                    f"stations CSV missing column(s): {', '.join(sorted(missing))}",
                    path=str(csv_path),
                    kind="invalid_stations",
                )
            seen: dict[str, int] = {}
            stations: list[Station] = []
            warnings: list[str] = []
            for line_no, row in enumerate(reader, start=2):
                station_id = (row.get("station_id") or "").strip()
                if not station_id:
                    raise SamplerError(
                        f"empty station_id on line {line_no}",
                        path=str(csv_path),
                        kind="invalid_stations",
                    )
                if station_id in seen:
                    raise SamplerError(
                        f"duplicate station_id {station_id!r} on lines "
                        f"{seen[station_id]} and {line_no}",
                        path=str(csv_path),
                        kind="invalid_stations",
                    )
                seen[station_id] = line_no
                try:
                    lat = float(row["lat"])
                    lon = float(row["lon"])
                except (TypeError, ValueError) as exc:
                    raise SamplerError(
                        f"invalid lat/lon for station {station_id!r} on line "
                        f"{line_no}: {exc}",
                        path=str(csv_path),
                        kind="invalid_stations",
                    ) from exc
                if not (math.isfinite(lat) and math.isfinite(lon)):
                    raise SamplerError(
                        f"non-finite lat/lon for station {station_id!r} on line "
                        f"{line_no}",
                        path=str(csv_path),
                        kind="invalid_stations",
                    )
                if not (-90.0 <= lat <= 90.0):
                    warnings.append(
                        f"station {station_id!r} lat {lat} is outside [-90, 90]"
                    )
                if not (-180.0 <= lon <= 180.0):
                    warnings.append(
                        f"station {station_id!r} lon {lon} is outside [-180, 180]"
                    )
                stations.append(Station(station_id, lat, lon))
    except OSError as exc:
        raise SamplerError(
            f"cannot read stations file: {exc}", path=str(csv_path),
            kind="file_error",
        ) from exc
    if not stations:
        raise SamplerError(
            "stations CSV contains no rows", path=str(csv_path),
            kind="invalid_stations",
        )
    return stations, warnings
