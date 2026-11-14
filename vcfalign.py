#!/usr/bin/env python3
"""vcfalign — align equivalent variants from small VCFs onto canonical keys.

The ``build`` command reads a reference FASTA plus one or more VCFs, checks
every record against the reference, left-normalises supported variants using
the reference sequence, and emits a canonical variant x sample long table
with full provenance and located diagnostics.

Supported scope — anything else is reported as "unsupported", never dropped
silently:
  * single-ALT records only (no multi-ALT projection)
  * SNVs and short pure insertions/deletions (event <= 50 bp)
  * diploid GT as the only FORMAT field (phase separator preserved)
  * simple FILTER: PASS/'.' vs any other single value

Outputs (written to --outdir):
  variants.csv      one row per canonical variant
  observations.csv  variant x sample long table (absent is a state, never 0/0)
  source-map.jsonl  one JSON object per accepted record x sample
  diagnostics.json  reference digest, support boundaries, errors,
                    unsupported records, conflicts, summary counts
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pysam

MAX_INDEL_LEN = 50
ALLELE_RE = re.compile(r"^[ACGTacgt]+$")
GT_SPLIT_RE = re.compile(r"[/|]")
PASS_VALUES = {".", "PASS", ""}

SUPPORT = {
    "alt_alleles": 1,
    "variant_kinds": ["snv", "insertion", "deletion"],
    "max_indel_len": MAX_INDEL_LEN,
    "ploidy": 2,
    "format_fields": ["GT"],
    "filter": "PASS or '.' (pass) vs any other single value (rejected)",
    "observation_states": [
        "hom_ref", "alt_carrier", "partial_missing",
        "missing", "filtered", "absent", "conflict",
    ],
}


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

def trim_common(pos0: int, ref: str, alt: str):
    """Strip removable common prefix/suffix; returns (pos0, ref, alt)."""
    while ref and alt and ref[0] == alt[0]:
        ref, alt, pos0 = ref[1:], alt[1:], pos0 + 1
    while ref and alt and ref[-1] == alt[-1]:
        ref, alt = ref[:-1], alt[:-1]
    return pos0, ref, alt


def left_normalize(pos0: int, ref: str, alt: str, refseq: str):
    """Canonical (pos1, ref, alt): trimmed, left-shifted through the
    reference, re-anchored to valid VCF (alleles never empty)."""
    pos0, ref, alt = trim_common(pos0, ref, alt)
    if ref and not alt:  # pure deletion: shift the deleted block leftwards
        while pos0 > 0 and refseq[pos0 - 1] == ref[-1]:
            ref = refseq[pos0 - 1] + ref[:-1]
            pos0 -= 1
    elif alt and not ref:  # pure insertion: shift the inserted block leftwards
        while pos0 > 0 and refseq[pos0 - 1] == alt[-1]:
            alt = refseq[pos0 - 1] + alt[:-1]
            pos0 -= 1
    if not ref or not alt:  # re-anchor on a reference base
        if pos0 > 0:
            anchor = refseq[pos0 - 1]
            return pos0, anchor + ref, anchor + alt
        right = pos0 + len(ref)
        if right >= len(refseq):
            raise ValueError("cannot anchor variant inside contig")
        anchor = refseq[right]
        return pos0 + 1, ref + anchor, alt + anchor
    return pos0 + 1, ref, alt


def variant_kind(ref: str, alt: str) -> str:
    if len(ref) == len(alt):
        return "snv"
    return "insertion" if len(alt) > len(ref) else "deletion"


def canonical_key(chrom: str, pos1: int, ref: str, alt: str) -> str:
    return f"{chrom}:{pos1}:{ref}>{alt}"


# --------------------------------------------------------------------------
# genotypes / filters
# --------------------------------------------------------------------------

def norm_filter(value: str) -> str:
    return "PASS" if value in PASS_VALUES else value


def classify_state(filter_str: str, gt: str) -> str:
    if filter_str not in PASS_VALUES:
        return "filtered"
    if gt == ".":
        return "missing"
    alleles = GT_SPLIT_RE.split(gt)
    n_missing = sum(1 for a in alleles if a == ".")
    if n_missing == len(alleles):
        return "missing"
    if n_missing:
        return "partial_missing"
    if all(a == "0" for a in alleles):
        return "hom_ref"
    return "alt_carrier"


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------

class Diagnostics:
    def __init__(self) -> None:
        self.errors: list[dict] = []
        self.unsupported: list[dict] = []
        self.conflicts: list[dict] = []

    @staticmethod
    def _entry(code, message, file, line, chrom=None, pos=None, extra=None):
        e = {"file": file, "line": line, "code": code, "message": message}
        if chrom is not None:
            e["chrom"] = chrom
        if pos is not None:
            e["pos"] = pos
        if extra:
            e.update(extra)
        return e

    def error(self, code, message, file, line, chrom=None, pos=None, **extra):
        self.errors.append(self._entry(code, message, file, line, chrom, pos, extra))

    def unsupp(self, code, message, file, line, chrom=None, pos=None, **extra):
        self.unsupported.append(self._entry(code, message, file, line, chrom, pos, extra))


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------

@dataclass
class Observation:
    source_id: str
    file: str
    line: int
    sample: str
    chrom: str
    pos: int
    ref: str
    alt: str
    gt: str
    filter: str
    state: str
    key: str


class Processor:
    def __init__(self, refseqs: dict[str, str]) -> None:
        self.refseqs = refseqs
        self.diag = Diagnostics()
        self.variants: dict[str, dict] = {}          # key -> canonical record
        self.cells: dict[tuple[str, str], list[Observation]] = {}
        self.source_entries: list[dict] = []

    # -- per-line -----------------------------------------------------------

    def process_line(self, path: str, lineno: int, line: str, samples: list[str]) -> bool:
        d = self.diag
        cols = line.split("\t")
        if len(cols) < 8:
            d.error("malformed_line",
                    f"expected at least 8 tab-separated columns, got {len(cols)}",
                    path, lineno)
            return False
        chrom, pos_s, _id, ref_raw, alt_field, _qual, filt, _info = cols[:8]

        try:
            pos = int(pos_s)
        except ValueError:
            d.error("bad_position", f"POS {pos_s!r} is not an integer",
                    path, lineno, chrom=chrom)
            return False

        if chrom not in self.refseqs:
            d.error("unknown_contig", f"contig {chrom!r} not present in reference",
                    path, lineno, chrom=chrom, pos=pos)
            return False
        refseq = self.refseqs[chrom]

        # ALT-level scope gates -------------------------------------------------
        if alt_field in ("", "."):
            d.unsupp("missing_alt", "ALT is '.' (no variant)", path, lineno,
                     chrom=chrom, pos=pos)
            return False
        if "," in alt_field:
            d.unsupp("multi_alt", f"multi-ALT record ({alt_field!r}) is out of scope",
                     path, lineno, chrom=chrom, pos=pos)
            return False
        alt = alt_field
        if alt == "*" or alt.startswith("<") or "[" in alt or "]" in alt:
            d.unsupp("symbolic_alt",
                     f"symbolic/structural ALT {alt!r} is out of scope",
                     path, lineno, chrom=chrom, pos=pos)
            return False

        # allele sanity ---------------------------------------------------------
        if not ALLELE_RE.match(ref_raw) or not ALLELE_RE.match(alt):
            d.error("illegal_allele",
                    f"alleles must be non-empty [ACGT] strings, got REF={ref_raw!r} ALT={alt!r}",
                    path, lineno, chrom=chrom, pos=pos)
            return False
        ref = ref_raw.upper()
        alt = alt.upper()
        if ref == alt:
            d.error("illegal_allele", f"REF equals ALT ({ref!r}); not a variant",
                    path, lineno, chrom=chrom, pos=pos)
            return False

        # coordinate checks -----------------------------------------------------
        pos0 = pos - 1
        if pos0 < 0 or pos0 + len(ref) > len(refseq):
            d.error("out_of_bounds",
                    f"REF spans {pos}..{pos + len(ref) - 1} but contig {chrom} "
                    f"is {len(refseq)} bp",
                    path, lineno, chrom=chrom, pos=pos)
            return False
        expected = refseq[pos0:pos0 + len(ref)]
        if expected != ref:
            d.error("ref_mismatch",
                    f"REF {ref!r} does not match reference {expected!r} at {chrom}:{pos}",
                    path, lineno, chrom=chrom, pos=pos,
                    expected=expected, observed=ref)
            return False

        # variant-class scope gates (on the trimmed event) ----------------------
        _p, t_ref, t_alt = trim_common(pos0, ref, alt)
        if t_ref and t_alt and not (len(t_ref) == len(t_alt) == 1):
            code = "mnp" if len(t_ref) == len(t_alt) else "complex_indel"
            d.unsupp(code,
                     f"trimmed event {t_ref}>{t_alt} is neither an SNV nor a pure "
                     f"insertion/deletion",
                     path, lineno, chrom=chrom, pos=pos)
            return False
        if max(len(t_ref), len(t_alt)) > MAX_INDEL_LEN:
            d.unsupp("indel_too_long",
                     f"indel event of {max(len(t_ref), len(t_alt))} bp exceeds "
                     f"{MAX_INDEL_LEN} bp limit",
                     path, lineno, chrom=chrom, pos=pos)
            return False

        # FORMAT / per-sample GT -------------------------------------------------
        gt_by_sample: dict[str, str] = {}
        if samples:
            if len(cols) < 9 + len(samples):
                d.error("malformed_line",
                        f"header lists {len(samples)} sample(s) but line has "
                        f"{max(0, len(cols) - 9)} sample column(s)",
                        path, lineno, chrom=chrom, pos=pos)
                return False
            fmt = cols[8]
            if fmt != "GT":
                d.unsupp("format_fields",
                         f"FORMAT {fmt!r} is out of scope; only 'GT' is supported",
                         path, lineno, chrom=chrom, pos=pos)
                return False
            for i, sample in enumerate(samples):
                gt = cols[9 + i]
                if gt != ".":
                    alleles = GT_SPLIT_RE.split(gt)
                    if len(alleles) != 2:
                        d.unsupp("ploidy",
                                 f"GT {gt!r} for sample {sample!r} is not diploid",
                                 path, lineno, chrom=chrom, pos=pos, sample=sample)
                        return False
                    bad = [a for a in alleles if a not in ("0", "1", ".")]
                    if bad:
                        d.error("illegal_genotype",
                                f"GT {gt!r} for sample {sample!r} has allele(s) "
                                f"outside 0/1/. (single-ALT scope)",
                                path, lineno, chrom=chrom, pos=pos, sample=sample)
                        return False
                gt_by_sample[sample] = gt

        # normalise against the reference ---------------------------------------
        try:
            n_pos, n_ref, n_alt = left_normalize(pos0, ref, alt, refseq)
        except ValueError as exc:
            d.error("normalize_failed", str(exc), path, lineno, chrom=chrom, pos=pos)
            return False
        key = canonical_key(chrom, n_pos, n_ref, n_alt)
        self.variants.setdefault(key, {
            "chrom": chrom, "pos": n_pos, "ref": n_ref, "alt": n_alt,
            "kind": variant_kind(n_ref, n_alt), "key": key,
        })

        for sample, gt in gt_by_sample.items():
            state = classify_state(filt, gt)
            obs = Observation(
                source_id=f"{path}:{lineno}#{sample}", file=path, line=lineno,
                sample=sample, chrom=chrom, pos=pos, ref=ref_raw, alt=alt_field,
                gt=gt, filter=filt, state=state, key=key,
            )
            self.cells.setdefault((key, sample), []).append(obs)
            self.source_entries.append({
                "source_id": obs.source_id, "file": path, "line": lineno,
                "sample": sample, "chrom": chrom, "pos": pos,
                "ref": ref_raw, "alt": alt_field, "gt": gt,
                "filter": norm_filter(filt), "state": state, "variant_key": key,
                "canonical": {"chrom": chrom, "pos": n_pos, "ref": n_ref,
                              "alt": n_alt, "key": key},
            })
        return True


# --------------------------------------------------------------------------
# input helpers
# --------------------------------------------------------------------------

def load_reference(path: Path) -> dict[str, str]:
    if not (Path(str(path) + ".fai").exists()):
        pysam.faidx(str(path))
    fasta = pysam.FastaFile(str(path))
    try:
        seqs = {name: fasta.fetch(name).upper() for name in fasta.references}
    finally:
        fasta.close()
    if not seqs:
        raise ValueError(f"no contigs found in reference {path}")
    return seqs


def header_samples(path: Path) -> list[str]:
    with pysam.VariantFile(str(path)) as vf:
        return [str(s) for s in vf.header.samples]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# build command
# --------------------------------------------------------------------------

def run_build(args: argparse.Namespace) -> int:
    ref_path = Path(args.reference)
    vcf_paths = [Path(p) for p in args.vcf]
    outdir = Path(args.outdir)

    for p in [ref_path, *vcf_paths]:
        if not p.is_file():
            print(f"error: input file not found: {p}", file=sys.stderr)
            return 2
    outdir.mkdir(parents=True, exist_ok=True)

    refseqs = load_reference(ref_path)
    proc = Processor(refseqs)

    inputs_meta = []
    all_samples: set[str] = set()
    for path in vcf_paths:
        path_s = str(path)
        try:
            samples = header_samples(path)
        except Exception as exc:
            proc.diag.error("bad_header", f"cannot parse VCF header: {exc}",
                            path_s, 0)
            inputs_meta.append({"path": path_s, "samples": [], "records_total": 0,
                                "records_accepted": 0, "records_rejected": 0})
            continue
        all_samples.update(samples)
        total = accepted = 0
        with open(path, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                line = raw.rstrip("\r\n")
                if not line or line.startswith("#"):
                    continue
                total += 1
                if proc.process_line(path_s, lineno, line, samples):
                    accepted += 1
        inputs_meta.append({"path": path_s, "samples": samples,
                            "records_total": total, "records_accepted": accepted,
                            "records_rejected": total - accepted})

    samples_sorted = sorted(all_samples)

    # canonical variant ids: sorted by locus, independent of input order
    keys_sorted = sorted(proc.variants, key=lambda k: (
        proc.variants[k]["chrom"], proc.variants[k]["pos"],
        proc.variants[k]["ref"], proc.variants[k]["alt"]))
    variant_id = {k: f"V{i}" for i, k in enumerate(keys_sorted, 1)}

    # ---- variants.csv ------------------------------------------------------
    variants_path = outdir / "variants.csv"
    with open(variants_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["variant_id", "chrom", "pos", "ref", "alt", "kind", "key"])
        for k in keys_sorted:
            v = proc.variants[k]
            w.writerow([variant_id[k], v["chrom"], v["pos"], v["ref"], v["alt"],
                        v["kind"], k])

    # ---- observations.csv + conflicts --------------------------------------
    observations_path = outdir / "observations.csv"
    state_counts: dict[str, int] = {}
    n_rows = 0
    with open(observations_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["variant_id", "sample", "state", "gt", "filter", "sources"])
        for k in keys_sorted:
            vid = variant_id[k]
            for sample in samples_sorted:
                n_rows += 1
                obs_list = proc.cells.get((k, sample), [])
                if not obs_list:
                    state, gt, filt, sources = "absent", "", "", ""
                else:
                    sigs = {(o.state, o.gt, norm_filter(o.filter)) for o in obs_list}
                    src = ";".join(sorted(o.source_id for o in obs_list))
                    if len(sigs) == 1:
                        state, gt, filt = next(iter(sigs))
                        filt = norm_filter(filt)
                        sources = src
                    else:
                        state, gt, filt, sources = "conflict", "", "", src
                        proc.diag.conflicts.append({
                            "variant_id": vid, "key": k, "sample": sample,
                            "reason": "inconsistent duplicate records for the "
                                      "same sample and canonical variant",
                            "records": [
                                {"source_id": o.source_id, "file": o.file,
                                 "line": o.line, "gt": o.gt,
                                 "filter": norm_filter(o.filter), "state": o.state}
                                for o in sorted(obs_list,
                                                key=lambda o: o.source_id)
                            ],
                        })
                state_counts[state] = state_counts.get(state, 0) + 1
                w.writerow([vid, sample, state, gt, filt, sources])

    # ---- source-map.jsonl ----------------------------------------------------
    for entry in proc.source_entries:
        entry["variant_id"] = variant_id[entry.pop("variant_key")]
    proc.source_entries.sort(key=lambda e: e["source_id"])
    sourcemap_path = outdir / "source-map.jsonl"
    with open(sourcemap_path, "w", encoding="utf-8") as fh:
        for entry in proc.source_entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ---- diagnostics.json ----------------------------------------------------
    diag = proc.diag
    diag.errors.sort(key=lambda e: (e["file"], e["line"], e["code"]))
    diag.unsupported.sort(key=lambda e: (e["file"], e["line"], e["code"]))
    diag.conflicts.sort(key=lambda e: (e["variant_id"], e["sample"]))
    diagnostics = {
        "reference": {
            "path": str(ref_path),
            "sha256": sha256_file(ref_path),
            "contigs": [
                {"name": name, "length": len(seq),
                 "sha256": hashlib.sha256(seq.encode()).hexdigest()}
                for name, seq in sorted(refseqs.items())
            ],
        },
        "support": SUPPORT,
        "samples": samples_sorted,
        "inputs": sorted(inputs_meta, key=lambda m: m["path"]),
        "errors": diag.errors,
        "unsupported": diag.unsupported,
        "conflicts": diag.conflicts,
        "summary": {
            "variants": len(keys_sorted),
            "samples": len(samples_sorted),
            "observations": n_rows,
            "observations_by_state": dict(sorted(state_counts.items())),
            "records_total": sum(m["records_total"] for m in inputs_meta),
            "records_accepted": sum(m["records_accepted"] for m in inputs_meta),
            "records_rejected": sum(m["records_rejected"] for m in inputs_meta),
            "errors": len(diag.errors),
            "unsupported": len(diag.unsupported),
            "conflicts": len(diag.conflicts),
        },
    }
    diagnostics_path = outdir / "diagnostics.json"
    with open(diagnostics_path, "w", encoding="utf-8") as fh:
        json.dump(diagnostics, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    s = diagnostics["summary"]
    print(f"built {s['variants']} variant(s) x {s['samples']} sample(s) "
          f"-> {s['observations']} observation(s)")
    print(f"records: {s['records_accepted']} accepted, {s['records_rejected']} rejected "
          f"({s['errors']} error(s), {s['unsupported']} unsupported), "
          f"{s['conflicts']} conflict(s)")
    for p in (variants_path, observations_path, sourcemap_path, diagnostics_path):
        print(f"wrote {p}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="vcfalign",
        description="Align equivalent variants from small VCFs onto canonical keys.")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build canonical variant x sample tables")
    b.add_argument("--reference", required=True, help="reference FASTA")
    b.add_argument("--vcf", action="append", required=True, metavar="VCF",
                   help="input VCF; repeat for multiple files")
    b.add_argument("--outdir", required=True, help="output directory")
    args = parser.parse_args(argv)
    if args.command == "build":
        return run_build(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
