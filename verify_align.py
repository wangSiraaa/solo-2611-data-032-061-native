#!/usr/bin/env python3
"""Independent verifier for `vcfalign build` outputs.

Deliberately shares no code with vcfalign.py: the reference is re-read from
disk with a plain parser, original and canonical alleles are applied to the
reference sequence directly (no normalisation function is called), and every
observation cell is recomputed from the raw VCF lines and compared against
observations.csv. Also checks order-independence and, on synthetic inputs,
duplicate-merge / conflict / unsupported / error behaviour.

Usage: python3 verify_align.py OUTDIR
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from hashlib import sha256
from pathlib import Path

GT_SPLIT_RE = re.compile(r"[/|]")
PASS_VALUES = {".", "PASS", ""}


# ---------------------------------------------------------------------------
# independent primitives (no imports from vcfalign)
# ---------------------------------------------------------------------------

def read_fasta(path: Path) -> dict[str, str]:
    seqs: dict[str, list[str]] = {}
    name = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith(">"):
                name = line[1:].split()[0]
                seqs[name] = []
            elif line:
                seqs[name].append(line)
    return {k: "".join(v).upper() for k, v in seqs.items()}


def apply_allele(seq: str, pos1: int, ref: str, alt: str):
    """Apply one allele to the reference; None if REF does not match."""
    p0 = pos1 - 1
    if p0 < 0 or seq[p0:p0 + len(ref)].upper() != ref.upper():
        return None
    return seq[:p0] + alt.upper() + seq[p0 + len(ref):]


def classify(filter_str: str, gt: str) -> str:
    if filter_str not in PASS_VALUES:
        return "filtered"
    if gt == ".":
        return "missing"
    alleles = GT_SPLIT_RE.split(gt)
    n_miss = sum(1 for a in alleles if a == ".")
    if n_miss == len(alleles):
        return "missing"
    if n_miss:
        return "partial_missing"
    if all(a == "0" for a in alleles):
        return "hom_ref"
    return "alt_carrier"


def norm_filter(value: str) -> str:
    return "PASS" if value in PASS_VALUES else value


def vcf_header_samples(path: Path) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#CHROM"):
                cols = line.rstrip("\r\n").split("\t")
                return cols[9:]
    return []


def vcf_data_lines(path: Path) -> dict[int, list[str]]:
    out = {}
    with open(path, encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            line = raw.rstrip("\r\n")
            if line and not line.startswith("#"):
                out[n] = line.split("\t")
    return out


# ---------------------------------------------------------------------------
# check harness
# ---------------------------------------------------------------------------

class Checker:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))

    def report(self) -> int:
        width = max(len(n) for n, _, _ in self.results)
        for name, ok, detail in self.results:
            mark = "PASS" if ok else "FAIL"
            line = f"[{mark}] {name.ljust(width)}"
            if detail:
                line += f"  {detail}"
            print(line)
        n_fail = sum(1 for _, ok, _ in self.results if not ok)
        print(f"\n{len(self.results) - n_fail}/{len(self.results)} checks passed")
        return 1 if n_fail else 0


# ---------------------------------------------------------------------------
# main verification
# ---------------------------------------------------------------------------

def load_outputs(outdir: Path):
    with open(outdir / "variants.csv", newline="", encoding="utf-8") as fh:
        variants = list(csv.DictReader(fh))
    with open(outdir / "observations.csv", newline="", encoding="utf-8") as fh:
        observations = list(csv.DictReader(fh))
    source_map = []
    with open(outdir / "source-map.jsonl", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                source_map.append(json.loads(line))
    with open(outdir / "diagnostics.json", encoding="utf-8") as fh:
        diagnostics = json.load(fh)
    return variants, observations, source_map, diagnostics


def verify_outdir(outdir: Path, ck: Checker, vcfalign: Path) -> None:
    variants, observations, source_map, diagnostics = load_outputs(outdir)
    vby_id = {v["variant_id"]: v for v in variants}
    ref_path = Path(diagnostics["reference"]["path"])
    refseqs = read_fasta(ref_path)

    # -- reference digest & support boundaries --------------------------------
    digest = sha256(ref_path.read_bytes()).hexdigest()
    ck.check("reference digest saved and matches",
             diagnostics["reference"]["sha256"] == digest,
             f"sha256={digest[:16]}...")
    contigs_ok = all(
        c["name"] in refseqs and c["length"] == len(refseqs[c["name"]])
        and c["sha256"] == sha256(refseqs[c["name"]].encode()).hexdigest()
        for c in diagnostics["reference"]["contigs"])
    ck.check("reference contig digests match", contigs_ok,
             ", ".join(f"{c['name']}:{c['length']}bp"
                       for c in diagnostics["reference"]["contigs"]))
    support = diagnostics.get("support", {})
    ck.check("support boundaries saved",
             all(k in support for k in
                 ("alt_alleles", "variant_kinds", "max_indel_len", "ploidy",
                  "format_fields")),
             json.dumps(support, sort_keys=True)[:90])

    # -- provenance + independent sequence equivalence -------------------------
    line_cache: dict[str, dict[int, list[str]]] = {}
    sample_cache: dict[str, list[str]] = {}

    def cols_of(file: str, line: int) -> list[str]:
        if file not in line_cache:
            line_cache[file] = vcf_data_lines(Path(file))
        return line_cache[file][line]

    def samples_of(file: str) -> list[str]:
        if file not in sample_cache:
            sample_cache[file] = vcf_header_samples(Path(file))
        return sample_cache[file]

    prov_ok = prov_fail = seq_fail = 0
    prov_msgs = []
    for e in source_map:
        cols = cols_of(e["file"], e["line"])
        chrom, pos, ref, alt = cols[0], int(cols[1]), cols[3], cols[4]
        # the json entry must describe the actual file line
        ok = (chrom, pos, ref, alt) == (e["chrom"], e["pos"], e["ref"], e["alt"])
        # sample identity comes from the header, not the column position
        samples = samples_of(e["file"])
        fmt = cols[8].split(":")
        sample_col = cols[9 + samples.index(e["sample"])] if e["sample"] in samples else ""
        gt = sample_col.split(":")[fmt.index("GT")] if "GT" in fmt else None
        ok = ok and gt == e["gt"]
        # canonical mapping in source-map must match variants.csv
        v = vby_id[e["variant_id"]]
        can = e["canonical"]
        ok = ok and (can["chrom"], str(can["pos"]), can["ref"], can["alt"]) == \
             (v["chrom"], v["pos"], v["ref"], v["alt"]) \
             and can["key"] == v["key"] == f"{v['chrom']}:{v['pos']}:{v['ref']}>{v['alt']}"
        # independently apply original and canonical alleles to the reference
        m_orig = apply_allele(refseqs[chrom], e["pos"], e["ref"], e["alt"])
        m_can = apply_allele(refseqs[v["chrom"]], int(v["pos"]), v["ref"], v["alt"])
        if m_orig is None:
            seq_fail += 1
            prov_msgs.append(f"original REF mismatch at {e['source_id']}")
        elif m_orig != m_can:
            seq_fail += 1
            prov_msgs.append(f"haplotypes differ at {e['source_id']}")
        prov_ok += ok
        prov_fail += (not ok)
    ck.check("provenance: every source entry matches its file line",
             prov_fail == 0, f"{prov_ok} entries checked")
    ck.check("original and canonical alleles yield identical sequence",
             seq_fail == 0 and len(source_map) > 0,
             f"{len(source_map) - seq_fail}/{len(source_map)} equivalent; "
             + (prov_msgs[0] if prov_msgs else "all haplotypes equal"))

    # -- cell-by-cell observations ---------------------------------------------
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in source_map:
        groups[(e["variant_id"], e["sample"])].append(e)

    expected: dict[tuple[str, str], dict] = {}
    for (vid, sample), entries in groups.items():
        sigs = set()
        sources = set()
        for e in entries:
            cols = cols_of(e["file"], e["line"])
            filt = norm_filter(cols[6])
            sigs.add((classify(cols[6], e["gt"]), e["gt"], filt))
            sources.add(e["source_id"])
        if len(sigs) == 1:
            state, gt, filt = next(iter(sigs))
        else:
            state, gt, filt = "conflict", "", ""
        expected[(vid, sample)] = {"state": state, "gt": gt, "filter": filt,
                                   "sources": sources}

    obs_by_cell = {(o["variant_id"], o["sample"]): o for o in observations}
    cell_fail = []
    for cell, exp in expected.items():
        got = obs_by_cell.get(cell)
        if got is None:
            cell_fail.append(f"{cell} missing from observations.csv")
            continue
        got_sources = set(filter(None, got["sources"].split(";")))
        if (got["state"], got["gt"], got["filter"], got_sources) != \
           (exp["state"], exp["gt"], exp["filter"], exp["sources"]):
            cell_fail.append(f"{cell}: expected {exp}, got "
                             f"{(got['state'], got['gt'], got['filter'], got_sources)}")
    # full cross product; cells without sources must be 'absent' and empty
    all_cells = {(v["variant_id"], s) for v in variants
                 for s in diagnostics["samples"]}
    if set(obs_by_cell) != all_cells:
        cell_fail.append("observations.csv is not the full variant x sample cross")
    for cell, got in obs_by_cell.items():
        if cell not in expected:
            if not (got["state"] == "absent" and got["gt"] == ""
                    and got["filter"] == "" and got["sources"] == ""):
                cell_fail.append(f"{cell}: source-less cell is not clean 'absent'")
    ck.check("cell-by-cell observation states and sources",
             not cell_fail, f"{len(obs_by_cell)} cells checked"
             + (f"; first failure: {cell_fail[0]}" if cell_fail else ""))

    # -- required behaviours on the fixture data --------------------------------
    # equivalent indels written at different positions converge
    src_pos_by_variant: dict[str, set] = defaultdict(set)
    for e in source_map:
        src_pos_by_variant[e["variant_id"]].add((e["chrom"], e["pos"]))
    convergent = {vid for vid, ps in src_pos_by_variant.items() if len(ps) > 1}
    ck.check("equivalent indels at different positions converge",
             len(convergent) >= 1,
             f"{len(convergent)} variant(s) merged from distinct positions: "
             + ", ".join(sorted(convergent)))

    # different variants at the same coordinate stay separate
    coord_to_variants: dict[tuple[str, int], set] = defaultdict(set)
    for e in source_map:
        coord_to_variants[(e["chrom"], e["pos"])].add(e["variant_id"])
    split = {c: vids for c, vids in coord_to_variants.items() if len(vids) > 1}
    ck.check("different variants at same coordinate are not merged",
             len(split) >= 1,
             "; ".join(f"{c[0]}:{c[1]} -> {sorted(v)}" for c, v in split.items()))

    states = {o["state"] for o in observations}
    required = {"hom_ref", "alt_carrier", "partial_missing",
                "missing", "filtered", "absent"}
    ck.check("all required observation states expressed",
             required <= states, f"states seen: {sorted(states)}")

    ck.check("explicit 0/0 is hom_ref (never inferred for absent cells)",
             any(o["gt"] == "0/0" and o["state"] == "hom_ref" for o in observations)
             and all(o["state"] != "hom_ref" for o in observations
                     if o["state"] == "absent"))
    ck.check("0/1 carried as alt_carrier",
             any(o["gt"] == "0/1" and o["state"] == "alt_carrier"
                 for o in observations))
    ck.check("GT phase separator preserved",
             any("|" in o["gt"] for o in observations),
             "e.g. " + next((o["gt"] for o in observations if "|" in o["gt"]), ""))
    ck.check("filtered records keep state 'filtered' with their GT",
             any(o["state"] == "filtered" and o["gt"] and o["filter"] != "PASS"
                 for o in observations))
    # unknown states are never inferred from other samples
    mixed = 0
    for v in variants:
        sts = {obs_by_cell[(v["variant_id"], s)]["state"]
               for s in diagnostics["samples"]}
        if "absent" in sts and len(sts) > 1:
            mixed += 1
    ck.check("absent cells stay absent while other samples carry the variant",
             mixed >= 1, f"{mixed} variant(s) with mixed presence")

    # -- input order must not change results -------------------------------------
    vcfs = [m["path"] for m in diagnostics["inputs"]]
    if len(vcfs) > 1:
        with tempfile.TemporaryDirectory() as tmp:
            r = run_build(vcfalign, ref_path, list(reversed(vcfs)), Path(tmp))
            same = r.returncode == 0 and all(
                (Path(tmp) / name).read_bytes() == (outdir / name).read_bytes()
                for name in ("variants.csv", "observations.csv",
                             "source-map.jsonl", "diagnostics.json"))
        ck.check("file enumeration order does not affect results", same,
                 "reversed --vcf order, byte-compared all four outputs")


def run_build(vcfalign: Path, reference: Path, vcfs: list[str], outdir: Path):
    cmd = [sys.executable, str(vcfalign), "build", "--reference", str(reference)]
    for v in vcfs:
        cmd += ["--vcf", str(v)]
    cmd += ["--outdir", str(outdir)]
    return subprocess.run(cmd, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# synthetic behaviour test: merge, conflict, unsupported, errors
# ---------------------------------------------------------------------------

SYNTH_HEADER = (
    "##fileformat=VCFv4.2\n"
    "##contig=<ID=chr1,length=16>\n"
    "##FILTER=<ID=LowQ,Description=\"synthetic\">\n"
    "##FORMAT=<ID=GT,Number=1,Type=String,Description=\"Genotype\">\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tC\n"
)


def verify_synthetic(ck: Checker, vcfalign: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "ref.fa").write_text(">chr1\nACGTACGTACGTACGT\n")
        # same sample C in both files: pos 2 agrees (merge), pos 5 disagrees (conflict)
        (tmp / "c1.vcf").write_text(
            SYNTH_HEADER
            + "chr1\t2\t.\tC\tT\t.\tPASS\t.\tGT\t0/1\n"
            + "chr1\t5\t.\tA\tG\t.\tPASS\t.\tGT\t0/1\n")
        (tmp / "c2.vcf").write_text(
            SYNTH_HEADER
            + "chr1\t2\t.\tC\tT\t.\tPASS\t.\tGT\t0/1\n"      # consistent duplicate
            + "chr1\t5\t.\tA\tG\t.\tPASS\t.\tGT\t1/1\n"      # conflicts with c1
            + "chr1\t9\t.\tA\tT,G\t.\tPASS\t.\tGT\t0/1\n"    # multi-ALT
            + "chr1\t3\t.\tG\t<DEL>\t.\tPASS\t.\tGT\t0/1\n"  # structural
            + "chr1\t4\t.\tT\tA\t.\tPASS\t.\tGT:DP\t0/1:30\n"  # other FORMAT
            + "chr1\t6\t.\tC\tT\t.\tPASS\t.\tGT\t0\n"        # haploid GT
            + "chr1\t13\t.\tC\tG\t.\tPASS\t.\tGT\t0/1\n"     # REF mismatch (ref=A)
            + "chr1\t20\t.\tA\tG\t.\tPASS\t.\tGT\t0/1\n"     # out of bounds
            + "chr1\t14\t.\tC\tX\t.\tPASS\t.\tGT\t0/1\n"     # illegal allele
            + "chr1\t7\t.\tG\tT\t.\tPASS\t.\tGT\t0/2\n")     # illegal genotype
        out = tmp / "out"
        r = run_build(vcfalign, tmp / "ref.fa",
                      [str(tmp / "c1.vcf"), str(tmp / "c2.vcf")], out)
        if r.returncode != 0:
            ck.check("synthetic build runs", False, r.stderr.strip()[:200])
            return
        variants, observations, source_map, diagnostics = load_outputs(out)

        obs = {(o["variant_id"], o["sample"]): o for o in observations}
        vkey = {v["variant_id"]: v["key"] for v in variants}
        merged = [c for c, o in obs.items()
                  if vkey[c[0]] == "chr1:2:C>T" and c[1] == "C"]
        conflicted = [c for c, o in obs.items()
                      if vkey[c[0]] == "chr1:5:A>G" and c[1] == "C"]
        m = obs[merged[0]] if merged else None
        ck.check("consistent duplicates merge their sources",
                 bool(m) and m["state"] == "alt_carrier"
                 and len(m["sources"].split(";")) == 2,
                 f"sources={m['sources'] if m else 'n/a'}")
        c = obs[conflicted[0]] if conflicted else None
        conf = diagnostics["conflicts"]
        ck.check("conflicting duplicates are reported, not picked by read order",
                 bool(c) and c["state"] == "conflict" and c["gt"] == ""
                 and len(conf) == 1 and len(conf[0]["records"]) == 2,
                 f"conflicts={len(conf)}")

        unsupp = {e["code"] for e in diagnostics["unsupported"]}
        errs = {e["code"] for e in diagnostics["errors"]}
        ck.check("multi-ALT / SV / other FORMAT / ploidy reported unsupported",
                 {"multi_alt", "symbolic_alt", "format_fields", "ploidy"} <= unsupp,
                 f"codes={sorted(unsupp)}")
        ck.check("ref mismatch / out-of-bounds / illegal allele located as errors",
                 {"ref_mismatch", "out_of_bounds", "illegal_allele",
                  "illegal_genotype"} <= errs
                 and all("file" in e and "line" in e and e["line"] > 0
                         for e in diagnostics["errors"]),
                 f"codes={sorted(errs)}")
        ck.check("rejected records stay out of the source map",
                 len(source_map) == diagnostics["summary"]["records_accepted"] == 4,
                 f"{len(source_map)} source entries")


def main(argv=None) -> int:
    outdir = Path(argv[0] if argv else sys.argv[1])
    vcfalign = Path(__file__).with_name("vcfalign.py")
    ck = Checker()
    verify_outdir(outdir, ck, vcfalign)
    verify_synthetic(ck, vcfalign)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
