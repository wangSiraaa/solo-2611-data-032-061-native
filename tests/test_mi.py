"""End-to-end tests: fixture run, small examples, invariance, reproducibility."""
from __future__ import annotations

import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from mi.dataset import CellState, compute_eligibility, load_dataset
from mi.errors import DiagnosticError
from mi.pipeline import run_pipeline
from mi.plan import load_plan
from mi.verify import verify_run

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
EXAMPLES = ROOT / "examples"


def _paths(d):
    return (d / "plan.json", d / "survey.csv", d / "applicability.csv")


def _read_completed(path):
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    header, body = rows[0], rows[1:]
    out = {}
    for r in body:
        for j, name in enumerate(header[1:]):
            out[(r[0], name)] = r[j + 1]
    return out


class TempDirMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)


class TestFixtureRun(TempDirMixin):
    """Main fixture: full pipeline + independent verification."""

    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp())
        cls.addClassCleanup(shutil.rmtree, cls.out, True)
        cls.res = run_pipeline(*_paths(FIXTURES), cls.out)

    def test_chain_statuses_and_fallback_event(self):
        for ch in self.res["chains"]:
            self.assertEqual(ch.status, "ok_with_flags")
            kinds = {e["kind"] for e in ch.events}
            # P000.x1 has an inapplicable predictor (x2): marginal fallback
            self.assertIn("marginal_fallback", kinds)

    def test_completed_datasets_are_not_copies(self):
        completed = [
            _read_completed(self.out / "completed" / f"completed_{k}.csv")
            for k in range(1, 6)
        ]
        for a in range(5):
            for b in range(a + 1, 5):
                self.assertNotEqual(completed[a], completed[b])

    def test_eligibility_record(self):
        elig = self.res["eligibility"]
        self.assertEqual(elig.n_eligible, 68)
        excluded = {self.res["dataset"].row_ids[i]
                    for i in range(72) if not elig.eligible[i]}
        self.assertEqual(excluded, {"P000", "P019", "P038", "P057"})

    def test_pooled_keeps_within_and_between(self):
        for r in self.res["pooled"]["results"]:
            self.assertIn("within_var", r)
            self.assertIn("between_var", r)
            self.assertGreater(r["between_var"], 0.0)
            self.assertAlmostEqual(
                r["total_var"],
                r["within_var"] + (1 + 1 / 5) * r["between_var"],
                places=12,
            )

    def test_complete_case_is_comparison_only(self):
        cc = self.res["complete_case"]
        self.assertIsNotNone(cc)
        self.assertEqual(cc.n, 52)  # 72 - 17 ordinary-missing rows - 3 more NA rows
        diag = json.loads((self.out / "diagnostics.json").read_text())
        self.assertTrue(any("对照" in lim or "comparison" in lim.lower()
                            for lim in diag["limitations"]))

    def test_independent_verification_passes(self):
        checks = verify_run(*_paths(FIXTURES), self.out)
        for name, ok, detail in checks:
            self.assertTrue(ok, f"{name}: {detail}")


class TestNoMissingExample(TempDirMixin):
    def test_pooled_equals_direct_fit_and_data_unchanged(self):
        out = self.tmp / "out"
        res = run_pipeline(*_paths(EXAMPLES / "no_missing"), out)
        plan, ds = res["plan"], res["dataset"]
        elig = res["eligibility"]

        # independent direct fit on the raw input
        row_idx = np.flatnonzero(elig.eligible)
        X = np.column_stack(
            [np.ones(len(row_idx)), ds.values["x1"][row_idx], ds.values["x2"][row_idx]]
        )
        beta, _, _, _ = np.linalg.lstsq(X, ds.values["outcome"][row_idx], rcond=None)
        got = np.array([r["estimate"] for r in res["pooled"]["results"]])
        np.testing.assert_allclose(got, beta, rtol=0, atol=1e-10)

        # no between-imputation variance, completed data identical to input
        for r in res["pooled"]["results"]:
            self.assertEqual(r["between_var"], 0.0)
            self.assertTrue(r["df_infinite"])
        for k in range(1, plan.n_datasets + 1):
            cells = _read_completed(out / "completed" / f"completed_{k}.csv")
            for i, rid in enumerate(ds.row_ids):
                for v in ds.variables:
                    self.assertEqual(float(cells[(rid, v)]), ds.values[v][i])

        checks = verify_run(*_paths(EXAMPLES / "no_missing"), out)
        self.assertTrue(all(ok for _, ok, _ in checks),
                        [d for _, ok, d in checks])


class TestRangeBoundExample(TempDirMixin):
    def test_imputed_values_within_declared_range(self):
        out = self.tmp / "out"
        res = run_pipeline(*_paths(EXAMPLES / "range_bound"), out)
        ds = res["dataset"]
        n_imp = 0
        for k in range(1, 5):
            cells = _read_completed(out / "completed" / f"completed_{k}.csv")
            for i, rid in enumerate(ds.row_ids):
                if ds.states["x1"][i] == CellState.MISSING:
                    x = float(cells[(rid, "x1")])
                    self.assertGreaterEqual(x, 0.0)
                    self.assertLessEqual(x, 10.0)
                    n_imp += 1
        self.assertEqual(n_imp, 4 * 8)
        checks = verify_run(*_paths(EXAMPLES / "range_bound"), out)
        self.assertTrue(all(ok for _, ok, _ in checks))


class TestDiagnosticFailures(TempDirMixin):
    def test_all_missing_column_refused_before_running(self):
        with self.assertRaises(DiagnosticError) as ctx:
            run_pipeline(*_paths(EXAMPLES / "all_missing_column"), self.tmp / "o1")
        msg = "; ".join(ctx.exception.findings)
        self.assertIn("x2", msg)
        self.assertIn("all-missing", msg)
        self.assertFalse((self.tmp / "o1").exists())

    def test_applicability_contradiction_refused(self):
        with self.assertRaises(DiagnosticError) as ctx:
            run_pipeline(*_paths(EXAMPLES / "contradiction"), self.tmp / "o2")
        self.assertIn("contradiction", "; ".join(ctx.exception.findings))

    def test_empty_analysis_sample_refused(self):
        # only 2 rows keep x2 applicable; the rest are structurally
        # not_applicable -> eligible rows cannot support the model
        d = self.tmp / "case"
        d.mkdir()
        shutil.copy(EXAMPLES / "no_missing" / "plan.json", d / "plan.json")
        with open(EXAMPLES / "no_missing" / "survey.csv", newline="") as fh:
            rows = list(csv.reader(fh))
        header = rows[0]
        x2_col = header.index("x2")
        keep = {rows[1][0], rows[2][0]}
        for r in rows[1:]:
            if r[0] not in keep:
                r[x2_col] = ""
        with open(d / "survey.csv", "w", newline="") as fh:
            csv.writer(fh, lineterminator="\n").writerows(rows)
        with open(d / "applicability.csv", "w", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(["row_id", "column", "status"])
            for r in rows[1:]:
                for v in ("x1", "x2", "outcome"):
                    na = v == "x2" and r[0] not in keep
                    w.writerow([r[0], v, "not_applicable" if na else "applicable"])
        with self.assertRaises(DiagnosticError) as ctx:
            run_pipeline(d / "plan.json", d / "survey.csv",
                         d / "applicability.csv", self.tmp / "o3")
        self.assertIn("analysis sample", "; ".join(ctx.exception.findings))


class TestCorrelatedMissingExample(TempDirMixin):
    def test_runs_with_real_between_variance(self):
        out = self.tmp / "out"
        res = run_pipeline(*_paths(EXAMPLES / "correlated_missing"), out)
        self.assertTrue(all(ch.status in ("ok", "ok_with_flags")
                            for ch in res["chains"]))
        for r in res["pooled"]["results"]:
            self.assertGreater(r["between_var"], 0.0)
        checks = verify_run(*_paths(EXAMPLES / "correlated_missing"), out)
        self.assertTrue(all(ok for _, ok, _ in checks))


class TestReproducibility(TempDirMixin):
    def _run_fixture(self, out, survey=None, applicability=None):
        return run_pipeline(
            FIXTURES / "plan.json",
            survey or FIXTURES / "survey.csv",
            applicability or FIXTURES / "applicability.csv",
            out,
        )

    def test_same_seed_identical_outputs(self):
        o1, o2 = self.tmp / "o1", self.tmp / "o2"
        self._run_fixture(o1)
        self._run_fixture(o2)
        for k in range(1, 6):
            self.assertEqual(
                (o1 / "completed" / f"completed_{k}.csv").read_bytes(),
                (o2 / "completed" / f"completed_{k}.csv").read_bytes(),
            )
        self.assertEqual(
            (o1 / "estimates" / "pooled.json").read_text(),
            (o2 / "estimates" / "pooled.json").read_text(),
        )

    def test_input_row_permutation_does_not_change_results(self):
        o1, o2 = self.tmp / "o1", self.tmp / "o2"
        self._run_fixture(o1)
        # shuffle survey rows and applicability rows with a fixed permutation
        rng = np.random.default_rng(999)
        for src_name in ("survey.csv", "applicability.csv"):
            with open(FIXTURES / src_name, newline="") as fh:
                rows = list(csv.reader(fh))
            header, body = rows[0], rows[1:]
            perm = rng.permutation(len(body))
            with open(self.tmp / src_name, "w", newline="") as fh:
                w = csv.writer(fh, lineterminator="\n")
                w.writerow(header)
                for i in perm:
                    w.writerow(body[i])
        self._run_fixture(o2, survey=self.tmp / "survey.csv",
                          applicability=self.tmp / "applicability.csv")
        for k in range(1, 6):
            self.assertEqual(
                (o1 / "completed" / f"completed_{k}.csv").read_bytes(),
                (o2 / "completed" / f"completed_{k}.csv").read_bytes(),
                f"completed dataset {k} changed under input permutation",
            )

    def test_different_seed_changes_imputations(self):
        o1, o2 = self.tmp / "o1", self.tmp / "o2"
        self._run_fixture(o1)
        plan = json.loads((FIXTURES / "plan.json").read_text())
        plan["seed"] = 125
        (self.tmp / "plan.json").write_text(json.dumps(plan))
        run_pipeline(self.tmp / "plan.json", FIXTURES / "survey.csv",
                     FIXTURES / "applicability.csv", o2)
        self.assertNotEqual(
            (o1 / "completed" / "completed_1.csv").read_bytes(),
            (o2 / "completed" / "completed_1.csv").read_bytes(),
        )


class TestPlanValidation(TempDirMixin):
    def _plan_with(self, **overrides):
        plan = json.loads((FIXTURES / "plan.json").read_text())
        plan.update(overrides)
        p = self.tmp / "plan.json"
        p.write_text(json.dumps(plan))
        return p

    def test_non_continuous_variable_rejected(self):
        plan = json.loads((FIXTURES / "plan.json").read_text())
        plan["variables"]["x1"]["type"] = "binary"
        p = self.tmp / "plan.json"
        p.write_text(json.dumps(plan))
        with self.assertRaises(DiagnosticError):
            load_plan(p)

    def test_single_dataset_rejected(self):
        with self.assertRaises(DiagnosticError):
            load_plan(self._plan_with(completed_datasets=1))

    def test_undeclared_survey_column_rejected(self):
        d = self.tmp
        shutil.copy(FIXTURES / "plan.json", d / "plan.json")
        shutil.copy(FIXTURES / "applicability.csv", d / "applicability.csv")
        with open(FIXTURES / "survey.csv", newline="") as fh:
            rows = list(csv.reader(fh))
        rows[0].append("junk")
        for r in rows[1:]:
            r.append("1.0")
        with open(d / "survey.csv", "w", newline="") as fh:
            csv.writer(fh, lineterminator="\n").writerows(rows)
        plan = load_plan(d / "plan.json")
        with self.assertRaises(DiagnosticError) as ctx:
            load_dataset(plan, d / "survey.csv", d / "applicability.csv")
        self.assertIn("junk", "; ".join(ctx.exception.findings))


if __name__ == "__main__":
    unittest.main()
