"""Per-cell local surface estimation.

Method (see METHOD.md for the full discussion):

1. Neighbourhood: all ground-candidate points within ``radius`` of the cell
   centre (single shared cKDTree). Fewer than ``min_support`` candidates ->
   the cell is reported as empty / insufficient support, never interpolated
   across.
2. Local surface: a plane z = a*dx + b*dy + c fitted in centre-relative
   coordinates, so the estimate is simply the intercept c. The fit is
   *low-seeded iterative sigma clipping*: start from the lowest
   ``low_z_fraction`` of points (ground is the lower envelope of a
   classified cloud), then repeatedly refit on points with
   |residual| <= outlier_k * sigma_inlier. Isolated high mislabeled
   "ground" points and two-level neighbourhoods (steps) are rejected as
   outliers instead of pulling the surface up.
3. Support geometry of the final inliers:
   * max angular gap of inlier directions > 180 deg  -> "one_sided"
     (the centre lies outside the convex hull of its support: extrapolation)
   * extrapolation_ratio = sqrt(n) * leverage of the centre > threshold
     -> "extrapolation" (lopsided support)
   * s_min/s_max of centred inlier XY < collinear_ratio -> "near_collinear"
     (surface tilt unconstrained along the weak axis)
   * many points rejected *above* the fitted surface -> "mixed_levels"
     (e.g. both sides of a step in one neighbourhood; the estimate follows
     the lower level and is flagged rather than averaged)
   Any of these demotes the cell to "low_confidence"; the estimate is kept
   but is explicitly not "reliable-looking".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

STATUS_OK = "ok"
STATUS_LOW = "low_confidence"
STATUS_INSUFFICIENT = "insufficient_support"
STATUS_EMPTY = "empty"
STATUSES = (STATUS_OK, STATUS_LOW, STATUS_INSUFFICIENT, STATUS_EMPTY)

#: statuses that carry a usable estimate (mask == True)
ESTIMATED_STATUSES = (STATUS_OK, STATUS_LOW)

_SIGMA_FLOOR_DEFAULT = 0.02  # m; keeps clipping stable on near-exact data


@dataclass(frozen=True)
class Params:
    radius: float = 3.0  # max neighbourhood radius [m]
    min_support: int = 6  # min ground candidates within radius
    min_inliers: int = 5  # min inliers required after outlier rejection
    outlier_k: float = 3.0  # inlier threshold = outlier_k * robust sigma
    clip_iters: int = 6  # max sigma-clipping iterations
    low_z_fraction: float = 0.5  # seed fit on this lowest fraction of points
    sigma_floor: float = _SIGMA_FLOOR_DEFAULT  # lower bound for robust sigma [m]
    extrapolation_max: float = 2.0  # sqrt(n)*leverage threshold
    collinear_ratio: float = 0.1  # s_min/s_max of centred inlier XY
    mixed_levels_min_frac: float = 0.2  # min fraction of high rejects -> mixed_levels
    mixed_levels_min_step: float = 0.15  # [m] high rejects below this height count as noise


@dataclass
class CellResult:
    row: int
    col: int
    x: float
    y: float
    status: str
    z: float = float("nan")
    reasons: tuple[str, ...] = ()
    candidate_count: int = 0  # ground candidates within radius
    support_count: int = 0  # inliers actually supporting the estimate
    support_ids: tuple[str, ...] = ()
    sigma: float = float("nan")  # robust inlier residual scale [m]
    max_abs_residual: float = float("nan")
    max_gap_deg: float = float("nan")
    extrapolation_ratio: float = float("nan")
    collinear_ratio: float = float("nan")


def _wls_plane(dx: np.ndarray, dy: np.ndarray, z: np.ndarray) -> np.ndarray:
    """Least-squares plane [a, b, c] through (dx, dy, z); used on inlier sets."""
    X = np.column_stack([dx, dy, np.ones(dx.size)])
    beta, *_ = np.linalg.lstsq(X, z, rcond=None)
    return beta


def _robust_sigma(r: np.ndarray) -> float:
    return 1.4826 * float(np.median(np.abs(r - np.median(r))))


def fit_local_plane(
    dx: np.ndarray, dy: np.ndarray, z: np.ndarray, params: Params
) -> tuple[np.ndarray, np.ndarray, float, np.ndarray]:
    """Low-seeded iterative sigma-clipping plane fit.

    Returns (beta, residuals, sigma, inlier_mask). Ground is treated as the
    lower envelope: the seed is the lowest ``low_z_fraction`` of points and
    clipping never lets high outliers back in.
    """
    n = z.size
    k0 = min(n, max(3, int(np.ceil(n * params.low_z_fraction))))
    order = np.argsort(z, kind="stable")
    inlier = np.zeros(n, dtype=bool)
    inlier[order[:k0]] = True
    beta = _wls_plane(dx[inlier], dy[inlier], z[inlier])

    for _ in range(params.clip_iters):
        r = z - (beta[0] * dx + beta[1] * dy + beta[2])
        sigma = max(_robust_sigma(r[inlier]), params.sigma_floor)
        keep = np.abs(r) <= params.outlier_k * sigma
        if keep.sum() < 3 or np.array_equal(keep, inlier):
            break
        inlier = keep
        beta = _wls_plane(dx[inlier], dy[inlier], z[inlier])

    r = z - (beta[0] * dx + beta[1] * dy + beta[2])
    sigma = max(_robust_sigma(r[inlier]), params.sigma_floor)
    return beta, r, sigma, inlier


def _support_geometry(
    dx: np.ndarray, dy: np.ndarray
) -> tuple[float, float, float]:
    """(max angular gap [rad], collinear ratio, extrapolation ratio)."""
    ang = np.arctan2(dy, dx)
    ang.sort()
    gaps = np.diff(np.concatenate([ang, ang[:1] + 2.0 * np.pi]))
    max_gap = float(gaps.max())

    xy = np.column_stack([dx, dy])
    xy = xy - xy.mean(axis=0)
    s = np.linalg.svd(xy, compute_uv=False)
    collinear = float(s[1] / s[0]) if s[0] > 1e-12 else 0.0

    X = np.column_stack([dx, dy, np.ones(dx.size)])
    G = np.linalg.pinv(X.T @ X)
    x0 = np.array([0.0, 0.0, 1.0])
    lev2 = float(x0 @ G @ x0)
    extrapolation = float(np.sqrt(max(lev2, 0.0) * dx.size))
    return max_gap, collinear, extrapolation


def estimate_cell(
    center: tuple[float, float],
    xy: np.ndarray,
    z: np.ndarray,
    ids,
    params: Params,
    row: int = -1,
    col: int = -1,
) -> CellResult:
    """Estimate ground elevation at ``center`` from candidate points (xy, z)."""
    cx, cy = center
    n = int(z.size)
    res = CellResult(row=row, col=col, x=cx, y=cy, status=STATUS_OK, candidate_count=n)
    if n == 0:
        res.status = STATUS_EMPTY
        return res
    if n < params.min_support:
        res.status = STATUS_INSUFFICIENT
        return res

    dx = xy[:, 0] - cx
    dy = xy[:, 1] - cy
    beta, r, sigma, inlier = fit_local_plane(dx, dy, z, params)
    k = int(inlier.sum())
    res.support_count = k
    if k < params.min_inliers:
        res.status = STATUS_INSUFFICIENT
        res.reasons = ("too_many_outliers",)
        return res

    max_gap, collinear, extrapolation = _support_geometry(dx[inlier], dy[inlier])
    reasons: list[str] = []
    if max_gap > np.pi:
        reasons.append("one_sided")
    if extrapolation > params.extrapolation_max:
        reasons.append("extrapolation")
    if collinear < params.collinear_ratio:
        reasons.append("near_collinear")
    # a *second surface level* (e.g. both sides of a step): points rejected
    # clearly above the fitted plane, beyond both the noise scale and a
    # material height difference
    high_rejects = int(
        np.sum(r > max(params.outlier_k * sigma, params.mixed_levels_min_step))
    )
    if high_rejects >= max(2, int(np.ceil(params.mixed_levels_min_frac * n))):
        reasons.append("mixed_levels")

    res.z = float(beta[2])
    res.status = STATUS_LOW if reasons else STATUS_OK
    res.reasons = tuple(reasons)
    res.support_ids = tuple(str(i) for i in np.asarray(ids, dtype=object)[inlier])
    res.sigma = float(sigma)
    res.max_abs_residual = float(np.abs(r[inlier]).max())
    res.max_gap_deg = float(np.degrees(max_gap))
    res.extrapolation_ratio = extrapolation
    res.collinear_ratio = collinear
    return res


def estimate_grid(
    grid,
    ground_ids: np.ndarray,
    ground_xyz: np.ndarray,
    params: Params,
    tree: cKDTree | None = None,
):
    """Estimate every cell. One KDTree is built (or reused) for all lookups.

    Returns (terrain, mask, results):
        terrain : (n_rows, n_cols) float64, NaN where mask is False
        mask    : (n_rows, n_cols) bool, True where an estimate exists
        results : list[CellResult] in row-major order
    """
    if tree is None:
        tree = cKDTree(ground_xyz[:, :2])
    terrain = np.full(grid.shape, np.nan, dtype=np.float64)
    mask = np.zeros(grid.shape, dtype=bool)
    results: list[CellResult] = []

    xs, ys = grid.axis_centers()
    for row, cy in enumerate(ys):
        for col, cx in enumerate(xs):
            idx = tree.query_ball_point([cx, cy], r=params.radius)
            if idx:
                sel = np.asarray(idx, dtype=np.int64)
                res = estimate_cell(
                    (cx, cy),
                    ground_xyz[sel, :2],
                    ground_xyz[sel, 2],
                    ground_ids[sel],
                    params,
                    row=row,
                    col=col,
                )
            else:
                res = estimate_cell(
                    (cx, cy),
                    np.empty((0, 2)),
                    np.empty(0),
                    [],
                    params,
                    row=row,
                    col=col,
                )
            results.append(res)
            if res.status in ESTIMATED_STATUSES:
                terrain[row, col] = res.z
                mask[row, col] = True
    return terrain, mask, results
