"""End-to-end tests.

Binary fixtures are produced here with an *independent* encoder (hand-rolled
nibble packing, no shared code with the decoder), then read back through the
converter.  Outputs are checked by an independent Arrow consumer
(pyarrow.parquet directly): types, values and reject counts, never just
file existence.
"""
from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from cobol_arrow import (CopybookError, Encoding, compile_copybook, convert_file,
                         explain_layout, explain_record, layout_json)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
ENC = Encoding.load(FIXTURES / "encoding.json")

# ---------------------------------------------------------------------------
# Independent encoder (test-only, does not reuse decoder code)
# ---------------------------------------------------------------------------

def enc_x(text: str, n: int) -> bytes:
    raw = text.encode("cp037")
    assert len(raw) <= n
    return raw + " ".encode("cp037") * (n - len(raw))  # cp037 space is 0x40


def enc_9(value: int, n: int) -> bytes:
    return f"{value:0{n}d}".encode("cp037")


def enc_comp3(value: int, digits: int, negative: bool = False) -> bytes:
    """value is the unscaled integer of `digits` decimal digits."""
    nibbles = [int(c) for c in f"{abs(value):0{digits}d}"]
    nibbles.append(0xD if negative else 0xC)
    if len(nibbles) % 2:
        nibbles.insert(0, 0)
    out = bytearray()
    for i in range(0, len(nibbles), 2):
        out.append(nibbles[i] << 4 | nibbles[i + 1])
    return bytes(out)


def record(data: bytes) -> bytes:
    total = len(data) + 4
    return total.to_bytes(4, "big") + data


def acct_fixed(acct: str, count: int, balance: int, neg: bool = False) -> bytes:
    return enc_x(acct, 8) + enc_9(count, 2) + enc_comp3(balance, 7, neg)


def acct_item(sku: str, qty: int, price: int, neg: bool = False) -> bytes:
    return enc_x(sku, 6) + enc_9(qty, 3) + enc_comp3(price, 5, neg)


# ---------------------------------------------------------------------------
# Provided fixture
# ---------------------------------------------------------------------------

def test_fixture_layout():
    layout = compile_copybook((FIXTURES / "account.cpy").read_text(), "account.cpy")
    assert layout.root.name == "ACCOUNT-RECORD"
    assert layout.fixed_length == 14
    assert layout.max_length == 50
    odo = layout.odo
    assert odo is not None and odo.path_string == "ACCOUNT-RECORD.DETAIL-LINES"
    assert odo.elem_length == 12 and odo.occurs.maximum == 3
    assert odo.occurs.dep_field.path_string == "ACCOUNT-RECORD.LINE-COUNT"
    # offsets are static because the dynamic area is the tail
    assert layout.named[("ACCOUNT-RECORD", "ACCOUNT-ID")].offset == 0
    assert layout.named[("ACCOUNT-RECORD", "LINE-COUNT")].offset == 8
    assert layout.named[("ACCOUNT-RECORD", "BALANCE")].offset == 10
    assert layout.named[("ACCOUNT-RECORD", "DETAIL-LINES")].offset == 14
    assert layout.named[("ACCOUNT-RECORD", "DETAIL-LINES", "PRICE")].offset == 23


def test_fixture_convert(tmp_path):
    layout = compile_copybook((FIXTURES / "account.cpy").read_text(), "account.cpy")
    out = tmp_path / "accounts.parquet"
    rej = tmp_path / "accounts.rejects.parquet"
    ok, bad = convert_file(layout, ENC, FIXTURES / "accounts.bin", out, rej)
    assert (ok, bad) == (3, 0)

    # independent Arrow consumer: check types, then values
    table = pq.read_table(out)
    schema = table.schema
    assert schema.field("ACCOUNT-ID").type == pa.string()
    assert schema.field("LINE-COUNT").type == pa.int64()
    assert schema.field("BALANCE").type == pa.decimal128(7, 2)
    detail = schema.field("DETAIL-LINES").type
    assert pa.types.is_list(detail) or pa.types.is_large_list(detail)
    item = detail.value_type
    assert pa.types.is_struct(item)
    assert item.field("SKU").type == pa.string()
    assert item.field("QUANTITY").type == pa.int64()
    assert item.field("PRICE").type == pa.decimal128(5, 2)

    rows = table.to_pylist()
    assert rows == [
        {"ACCOUNT-ID": "A000001 ", "LINE-COUNT": 2, "BALANCE": Decimal("1234.56"),
         "DETAIL-LINES": [
             {"SKU": "P01   ", "QUANTITY": 2, "PRICE": Decimal("12.34")},
             {"SKU": "P02   ", "QUANTITY": 10, "PRICE": Decimal("5.99")}]},
        {"ACCOUNT-ID": "A000002 ", "LINE-COUNT": 0, "BALANCE": Decimal("-30.00"),
         "DETAIL-LINES": []},
        {"ACCOUNT-ID": "A000003 ", "LINE-COUNT": 3, "BALANCE": Decimal("0.00"),
         "DETAIL-LINES": [
             {"SKU": "P10   ", "QUANTITY": 1, "PRICE": Decimal("0.00")},
             {"SKU": "P11   ", "QUANTITY": 4, "PRICE": Decimal("-0.25")},
             {"SKU": "P12   ", "QUANTITY": 999, "PRICE": Decimal("999.99")}]},
    ]
    rejects = pq.read_table(rej)
    assert rejects.num_rows == 0
    assert rejects.schema.names == ["record_index", "file_offset", "field_path",
                                    "error_code", "message", "record_hex"]


# ---------------------------------------------------------------------------
# Generated cases: zero/max occurrences, negatives, bad nibbles, truncation
# ---------------------------------------------------------------------------

def _generated_file() -> bytes:
    blob = b""
    # 0: zero occurrences
    blob += record(acct_fixed("T0000001", 0, 0))
    # 1: max occurrences (3) with a negative balance and a negative price
    blob += record(acct_fixed("T0000002", 3, 1234567, neg=True)
                   + acct_item("S01", 1, 1)
                   + acct_item("S02", 20, 250, neg=True)
                   + acct_item("S03", 300, 99999))
    # 2: one occurrence
    blob += record(acct_fixed("T0000003", 1, 5) + acct_item("S09", 7, 42))
    # 3: bad packed digit nibble (0xA) in PRICE of item 2
    bad_price = enc_x("S11", 6) + enc_9(1, 3) + bytes([0x01, 0x2A, 0x4C])
    blob += record(acct_fixed("T0000004", 2, 10) + acct_item("S10", 1, 5) + bad_price)
    # 4: non-digit byte in the DISPLAY counter
    blob += record(enc_x("T0000005", 8) + b"\xf0\x40" + enc_comp3(1, 7)
                   + acct_item("S12", 1, 1))
    # 5: count field says 2 but the record carries 1 item
    blob += record(acct_fixed("T0000006", 2, 1) + acct_item("S13", 1, 1))
    # 6: bad sign nibble 0xB in PRICE
    bad_sign = enc_x("S14", 6) + enc_9(2, 3) + bytes([0x00, 0x01, 0x0B])
    blob += record(acct_fixed("T0000007", 1, 1) + bad_sign)
    # 7: record shorter than the fixed layout
    blob += record(b"\xc1\xf0")
    # 8: truncated: prefix declares 40 data bytes, only 10 follow (must be last)
    blob += (44).to_bytes(4, "big") + acct_fixed("T0000008", 0, 0)[:10]
    return blob


def test_generated_cases(tmp_path):
    layout = compile_copybook((FIXTURES / "account.cpy").read_text(), "account.cpy")
    src = tmp_path / "gen.bin"
    src.write_bytes(_generated_file())
    out, rej = tmp_path / "gen.parquet", tmp_path / "gen.rejects.parquet"
    ok, bad = convert_file(layout, ENC, src, out, rej)
    assert (ok, bad) == (3, 6)

    rows = pq.read_table(out).to_pylist()
    assert rows[0] == {"ACCOUNT-ID": "T0000001", "LINE-COUNT": 0,
                       "BALANCE": Decimal("0.00"), "DETAIL-LINES": []}
    assert rows[1] == {"ACCOUNT-ID": "T0000002", "LINE-COUNT": 3,
                       "BALANCE": Decimal("-12345.67"),
                       "DETAIL-LINES": [
                           {"SKU": "S01   ", "QUANTITY": 1, "PRICE": Decimal("0.01")},
                           {"SKU": "S02   ", "QUANTITY": 20, "PRICE": Decimal("-2.50")},
                           {"SKU": "S03   ", "QUANTITY": 300, "PRICE": Decimal("999.99")}]}
    assert rows[2]["ACCOUNT-ID"] == "T0000003"
    # the truncated record must not have produced a partial row
    assert all(r["ACCOUNT-ID"] != "T0000008" for r in rows)

    rejects = pq.read_table(rej).to_pylist()
    assert len(rejects) == 6
    by_index = {r["record_index"]: r for r in rejects}
    assert by_index[3]["error_code"] == "BAD_DIGIT"
    assert by_index[3]["field_path"] == "ACCOUNT-RECORD.DETAIL-LINES[2].PRICE"
    assert by_index[4]["error_code"] == "BAD_DIGIT"
    assert by_index[4]["field_path"] == "ACCOUNT-RECORD.LINE-COUNT"
    assert by_index[5]["error_code"] == "COUNT_MISMATCH"
    assert by_index[5]["field_path"] == "ACCOUNT-RECORD.DETAIL-LINES"
    assert by_index[6]["error_code"] == "BAD_SIGN"
    assert by_index[6]["field_path"] == "ACCOUNT-RECORD.DETAIL-LINES[1].PRICE"
    assert by_index[7]["error_code"] == "SHORT_RECORD"
    assert by_index[7]["field_path"] is None
    assert by_index[8]["error_code"] == "TRUNCATED"
    assert by_index[8]["field_path"] is None
    # rejects carry the file offset of the failing record
    offsets = [r["file_offset"] for r in rejects]
    assert offsets == sorted(offsets) and all(o >= 0 for o in offsets)


def test_bad_prefix_stops_scan(tmp_path):
    layout = compile_copybook((FIXTURES / "account.cpy").read_text(), "account.cpy")
    good = record(acct_fixed("T0000001", 0, 0))
    src = tmp_path / "bad.bin"
    src.write_bytes(good + (65536).to_bytes(4, "big") + good)  # prefix > max_record_bytes
    out, rej = tmp_path / "o.parquet", tmp_path / "r.parquet"
    ok, bad = convert_file(layout, ENC, src, out, rej)
    assert (ok, bad) == (1, 1)  # scan stops at the corrupt prefix
    rejects = pq.read_table(rej).to_pylist()
    assert rejects[0]["error_code"] == "BAD_PREFIX"
    assert rejects[0]["record_index"] == 1
    assert rejects[0]["file_offset"] == len(good)


# ---------------------------------------------------------------------------
# Subset generality: FILLER, nesting, fixed OCCURS, ODO inside a tail group
# ---------------------------------------------------------------------------

GENERAL_CPY = """
      * exercise the rest of the subset
       01 GENERAL-REC.
          05 FLAG PIC X.
          05 FILLER PIC X(4).
          05 META.
             10 CODE PIC 9(2).
             10 FILLER PIC XX.
          05 TAGS PIC X(2) OCCURS 3 TIMES.
          05 ROWS OCCURS 2 TIMES.
             10 NUM PIC 9(2).
             10 TXT PIC X(3).
          05 TOTAL PIC S9(3)V9(2) COMP-3.
"""

GENERAL_LAYOUT_FACTS = {"fixed": 1 + 4 + 2 + 2 + 3 * 2 + 2 * 5 + 3}


def test_fixed_occurs_nested_filler(tmp_path):
    layout = compile_copybook(GENERAL_CPY, "general.cpy")
    assert layout.odo is None
    assert layout.fixed_length == GENERAL_LAYOUT_FACTS["fixed"]
    assert ("GENERAL-REC", "FILLER") not in layout.named  # FILLER has no path

    row1 = (enc_x("A", 1) + b"\x00" * 4 + enc_9(7, 2) + b"\xff" * 2
            + enc_x("t1", 2) + enc_x("t2", 2) + enc_x("t3", 2)
            + enc_9(1, 2) + enc_x("abc", 3) + enc_9(2, 2) + enc_x("def", 3)
            + enc_comp3(12345, 5, negative=True))
    row2 = (enc_x("B", 1) + b"\x00" * 4 + enc_9(0, 2) + b"\xff" * 2
            + enc_x("xx", 2) + enc_x("yy", 2) + enc_x("zz", 2)
            + enc_9(3, 2) + enc_x("ghi", 3) + enc_9(4, 2) + enc_x("jkl", 3)
            + enc_comp3(1, 5))
    src = tmp_path / "g.bin"
    src.write_bytes(record(row1) + record(row2))
    out, rej = tmp_path / "g.parquet", tmp_path / "g.rejects.parquet"
    ok, bad = convert_file(layout, ENC, src, out, rej)
    assert (ok, bad) == (2, 0)

    table = pq.read_table(out)
    schema = table.schema
    assert "FILLER" not in schema.names
    meta = schema.field("META").type
    assert pa.types.is_struct(meta) and meta.field("CODE").type == pa.int64()
    tags = schema.field("TAGS").type
    assert pa.types.is_fixed_size_list(tags) and tags.list_size == 3
    rows_t = schema.field("ROWS").type
    assert pa.types.is_fixed_size_list(rows_t) and rows_t.list_size == 2
    assert pa.types.is_struct(rows_t.value_type)
    assert schema.field("TOTAL").type == pa.decimal128(5, 2)

    rows = table.to_pylist()
    assert rows[0] == {"FLAG": "A", "META": {"CODE": 7},
                       "TAGS": ["t1", "t2", "t3"],
                       "ROWS": [{"NUM": 1, "TXT": "abc"}, {"NUM": 2, "TXT": "def"}],
                       "TOTAL": Decimal("-123.45")}
    assert rows[1]["TOTAL"] == Decimal("0.01")


def test_odo_inside_tail_group(tmp_path):
    cpy = """
       01 R.
          05 HEAD PIC 9(2).
          05 TAIL.
             10 N PIC 9(1).
             10 ROWS OCCURS 0 TO 4 TIMES
                DEPENDING ON N OF TAIL.
                15 V PIC 9(2).
    """
    layout = compile_copybook(cpy, "tail.cpy")
    assert layout.odo is not None
    assert layout.odo.path_string == "R.TAIL.ROWS"
    assert layout.fixed_length == 3 and layout.max_length == 3 + 4 * 2

    data = enc_9(5, 2) + enc_9(2, 1) + enc_9(11, 2) + enc_9(22, 2)
    src = tmp_path / "t.bin"
    src.write_bytes(record(data))
    out, rej = tmp_path / "t.parquet", tmp_path / "t.rejects.parquet"
    ok, bad = convert_file(layout, ENC, src, out, rej)
    assert (ok, bad) == (1, 0)
    rows = pq.read_table(out).to_pylist()
    assert rows == [{"HEAD": 5, "TAIL": {"N": 2, "ROWS": [{"V": 11}, {"V": 22}]}}]


# ---------------------------------------------------------------------------
# Compile-time rejections (each must carry a line/column)
# ---------------------------------------------------------------------------

ERROR_CASES = {
    "signed display": "01 R.\n   05 A PIC S9(3).\n",
    "display with V": "01 R.\n   05 A PIC 9(3)V9(2).\n",
    "unsigned comp3": "01 R.\n   05 A PIC 9(3) COMP-3.\n",
    "redefines": "01 R.\n   05 A PIC X(2).\n   05 B REDEFINES A PIC X(2).\n",
    "value clause": "01 R.\n   05 A PIC X(2) VALUE 'XX'.\n",
    "level 88": "01 R.\n   05 A PIC X(2).\n   88 B VALUE 'X'.\n",
    "child of elementary": "01 R.\n   05 A PIC X(1).\n   10 B PIC X(1).\n",
    "duplicate sibling": "01 R.\n   05 A PIC X(1).\n   05 A PIC X(2).\n",
    "duplicate via filler": "01 R.\n   05 FILLER.\n      10 A PIC X(1).\n   05 A PIC X(1).\n",
    "two roots": "01 R.\n   05 A PIC X(1).\n01 S.\n   05 B PIC X(1).\n",
    "root with pic": "01 R PIC X(4).\n",
    "odo not last": "01 R.\n   05 N PIC 9(1).\n   05 G OCCURS 0 TO 2 DEPENDING ON N.\n"
                    "      10 X PIC 9(1).\n   05 Z PIC X(1).\n",
    "two odos": "01 R.\n   05 N PIC 9(1).\n   05 G OCCURS 0 TO 2 DEPENDING ON N.\n"
                "      10 X PIC 9(1).\n",
    "odo in fixed occurs": "01 R.\n   05 N PIC 9(1).\n"
                           "   05 G OCCURS 2 TIMES.\n"
                           "      10 H OCCURS 0 TO 2 DEPENDING ON N.\n"
                           "         15 X PIC 9(1).\n",
    "dep forward": "01 R.\n   05 G OCCURS 0 TO 2 DEPENDING ON N.\n      10 X PIC 9(1).\n",
    "dep ambiguous": "01 R.\n   05 G1.\n      10 N PIC 9(1).\n   05 G2.\n      10 N PIC 9(1).\n"
                     "   05 H OCCURS 0 TO 2 DEPENDING ON N.\n      10 X PIC 9(1).\n",
    "dep under occurs": "01 R.\n   05 G OCCURS 2 TIMES.\n      10 N PIC 9(1).\n"
                        "   05 H OCCURS 0 TO 2 DEPENDING ON N.\n      10 X PIC 9(1).\n",
    "dep not integer": "01 R.\n   05 N PIC X(1).\n"
                       "   05 H OCCURS 0 TO 2 DEPENDING ON N.\n      10 X PIC 9(1).\n",
    "empty group": "01 R.\n   05 G.\n   05 A PIC X(1).\n",
    "bad pic char": "01 R.\n   05 A PIC A(3).\n",
    "mixed pic": "01 R.\n   05 A PIC X9X.\n",
    "occurs on root": "01 R OCCURS 2 TIMES.\n   05 A PIC X(1).\n",
    "usage comp": "01 R.\n   05 A PIC 9(3) COMP.\n",
    "odo lower bound": "01 R.\n   05 N PIC 9(1).\n"
                       "   05 G OCCURS 1 TO 2 DEPENDING ON N.\n      10 X PIC 9(1).\n",
}


@pytest.mark.parametrize("name", sorted(ERROR_CASES))
def test_compile_errors(name):
    # "two odos" needs a second ODO; patch the case text
    text = ERROR_CASES[name]
    if name == "two odos":
        text = ("01 R.\n   05 N PIC 9(1).\n   05 M PIC 9(1).\n"
                "   05 G OCCURS 0 TO 2 DEPENDING ON N.\n      10 X PIC 9(1).\n"
                "   05 H OCCURS 0 TO 2 DEPENDING ON M.\n      10 Y PIC 9(1).\n")
    with pytest.raises(CopybookError) as excinfo:
        compile_copybook(text, f"{name}.cpy")
    err = excinfo.value
    assert err.line >= 1 and err.col >= 1
    assert f"{name}.cpy:{err.line}:{err.col}" in str(err)


def test_multiline_and_comments():
    text = ("      * leading comment\n"
            "       01 R.\n"
            "          05 N PIC 9(1). *> trailing comment\n"
            "          05 G OCCURS 0 TO 2 TIMES\n"
            "             DEPENDING ON N.\n"
            "             10 X PIC 9(2).\n")
    layout = compile_copybook(text, "comments.cpy")
    assert layout.odo is not None
    assert layout.fixed_length == 1


# ---------------------------------------------------------------------------
# Encoding declaration variants
# ---------------------------------------------------------------------------

def test_encoding_variant_little_endian_short_prefix(tmp_path):
    """The converter must follow the encoding declaration, not hardcode it."""
    from cobol_arrow import Encoding as Enc
    decl = tmp_path / "enc.json"
    decl.write_text(json.dumps({
        "character_encoding": "cp037",
        "record_prefix": {"bytes": 2, "byte_order": "little", "includes_prefix": False},
        "packed_decimal": {"positive_signs": ["C", "F"], "negative_sign": "D"},
        "max_record_bytes": 1024,
    }))
    enc = Enc.load(decl)
    layout = compile_copybook(GENERAL_CPY, "general.cpy")
    row = (enc_x("A", 1) + b"\x00" * 4 + enc_9(7, 2) + b"\xff" * 2
           + enc_x("t1", 2) + enc_x("t2", 2) + enc_x("t3", 2)
           + enc_9(1, 2) + enc_x("abc", 3) + enc_9(2, 2) + enc_x("def", 3)
           + enc_comp3(12345, 5, negative=True))
    src = tmp_path / "v.bin"
    src.write_bytes(len(row).to_bytes(2, "little") + row)  # prefix excludes itself
    out, rej = tmp_path / "v.parquet", tmp_path / "v.rejects.parquet"
    ok, bad = convert_file(layout, enc, src, out, rej)
    assert (ok, bad) == (1, 0)
    assert pq.read_table(out).to_pylist()[0]["TOTAL"] == Decimal("-123.45")


def test_encoding_declaration_validation(tmp_path):
    from cobol_arrow import Encoding as Enc, EncodingError
    with pytest.raises(EncodingError):
        Enc.load(FIXTURES / "does-not-exist.json")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"packed_decimal": {"positive_signs": ["D"],
                                                  "negative_sign": "D"}}))
    with pytest.raises(EncodingError):
        Enc.load(bad)
    bad.write_text(json.dumps({"record_prefix": {"bytes": 9}}))
    with pytest.raises(EncodingError):
        Enc.load(bad)


# ---------------------------------------------------------------------------
# explain-layout / explain-record
# ---------------------------------------------------------------------------

def test_explain_layout_text_and_json():
    layout = compile_copybook((FIXTURES / "account.cpy").read_text(), "account.cpy")
    text = explain_layout(layout)
    assert "explain-layout" in text
    assert "fixed region : 14" in text
    assert "DEPENDING ON ACCOUNT-RECORD.LINE-COUNT" in text
    doc = json.loads(layout_json(layout))
    assert doc["fixed_length"] == 14 and doc["max_length"] == 50
    assert doc["odo"] == {"path": "ACCOUNT-RECORD.DETAIL-LINES",
                          "count_path": "ACCOUNT-RECORD.LINE-COUNT",
                          "max": 3, "item_length": 12, "max_length": 36}
    assert doc["paths"]["ACCOUNT-RECORD.BALANCE"] == {
        "offset": 10, "length": 4, "kind": "comp3", "occurs": None}


def test_explain_record_fixture():
    layout = compile_copybook((FIXTURES / "account.cpy").read_text(), "account.cpy")
    bin_path = FIXTURES / "accounts.bin"
    out = explain_record(layout, ENC, bin_path, 0, "DETAIL-LINES[2].PRICE")
    assert "file [39, 42)" in out and "00599c" in out and "5.99" in out
    out = explain_record(layout, ENC, bin_path, 1, "BALANCE")
    assert "file [56, 60)" in out and "0003000d" in out and "-30.00" in out
    out = explain_record(layout, ENC, bin_path, 2, "DETAIL-LINES[3].QUANTITY")
    assert "file [108, 111)" in out and "f9f9f9" in out
    out = explain_record(layout, ENC, bin_path, 0, "ACCOUNT-RECORD.ACCOUNT-ID")
    assert "file [4, 12)" in out and "'A000001 '" in out


def test_explain_record_errors(tmp_path):
    layout = compile_copybook((FIXTURES / "account.cpy").read_text(), "account.cpy")
    bin_path = FIXTURES / "accounts.bin"
    from cobol_arrow.explain import UserError
    with pytest.raises(UserError):
        explain_record(layout, ENC, bin_path, 0, "DETAIL-LINES[4].PRICE")  # only 2 items
    with pytest.raises(UserError):
        explain_record(layout, ENC, bin_path, 0, "NOPE")
    with pytest.raises(UserError):
        explain_record(layout, ENC, bin_path, 9, "ACCOUNT-ID")  # no such record
    # a rejected record reports its rejection instead of field bytes
    src = tmp_path / "bad.bin"
    src.write_bytes(record(acct_fixed("T0000006", 2, 1) + acct_item("S13", 1, 1)))
    out = explain_record(layout, ENC, src, 0, "ACCOUNT-ID")
    assert "rejected" in out and "COUNT_MISMATCH" in out


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------

def _run_cli(*args):
    return subprocess.run([sys.executable, "-m", "cobol_arrow", *args],
                          capture_output=True, text=True, cwd=ROOT)


def test_cli_compile_convert_explain(tmp_path):
    res = _run_cli("compile", "--copybook", "fixtures/account.cpy")
    assert res.returncode == 0 and "explain-layout" in res.stdout
    res = _run_cli("compile", "--copybook", "fixtures/account.cpy", "--json")
    assert res.returncode == 0 and json.loads(res.stdout)["record"] == "ACCOUNT-RECORD"

    out = tmp_path / "a.parquet"
    res = _run_cli("convert", "--copybook", "fixtures/account.cpy",
                   "--input", "fixtures/accounts.bin",
                   "--encoding", "fixtures/encoding.json", "--out", str(out))
    assert res.returncode == 0, res.stderr
    assert "converted 3 record(s), rejected 0" in res.stdout
    assert pq.read_table(out).num_rows == 3
    assert pq.read_table(tmp_path / "a.rejects.parquet").num_rows == 0

    res = _run_cli("explain-record", "--copybook", "fixtures/account.cpy",
                   "--input", "fixtures/accounts.bin", "--record", "0",
                   "--field", "DETAIL-LINES[2].PRICE")
    assert res.returncode == 0 and "5.99" in res.stdout


def test_cli_compile_error_position(tmp_path):
    bad = tmp_path / "bad.cpy"
    bad.write_text("01 R.\n   05 A PIC X(2).\n   05 B REDEFINES A PIC X(2).\n")
    res = _run_cli("compile", "--copybook", str(bad))
    assert res.returncode == 2
    assert "bad.cpy:3:" in res.stderr and "REDEFINES" in res.stderr
