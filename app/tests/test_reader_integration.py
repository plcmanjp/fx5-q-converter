"""Exercise the actual bundled reader through the app with synthetic carriers."""
import io
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import zipfile

from converter import core, upstream_contracts


def database(script):
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(script)
        return connection.serialize()


def positioning_mil(width="A32", marker="PLSV"):
    if marker == "PLSV":
        return ("V1:1:1:1:PLSV:D:Y:Y:ms{el=[mc{op=cl{op=#:ct=a:as=["
                f"as{{vt={width}}}:as{{vt=Aea}}:as{{vt=Abl}}]}}:"
                "as=[d{s=#:a=12:vt=nn}:d{s=#:a=16:vt=nn}:d{s=#:a=20:vt=nn}]}]}")
    return (f"V1:1:1:1:{marker}:D:D:K_1:M:ms{{el=[mc{{op=cl{{op=#:ct=a:as=["
            f"as{{vt={width}}}:as{{vt={width}}}:as{{vt=Aea}}:as{{vt=Abl}}]}}:"
            "as=[d{s=#:a=12:vt=nn}:d{s=#:a=24:vt=nn}:c{s=#:v=3:si=s}:d{s=#:a=40:vt=nn}]}]}")


def synthetic_source(width):
    mil = positioning_mil(width)
    name = "MAIN\0".encode("utf-16le")
    entries = {
        "Config.xml": b'<Config Unit="FX5U" UnitId="528" Title="Synthetic"/>',
        "ConvertData/1/PouLinkOrder.info": b"1\n",
        "ConvertData/1/Program.qpg": len(name).to_bytes(2, "little") + name,
        "ConvertData/1/PouPCode.pcode": b"synthetic",
        "SYN_LDDB.db": database("CREATE TABLE LadderBlocks(id TEXT,pos REAL,blocktype INTEGER,data TEXT); INSERT INTO LadderBlocks VALUES('block',0,0,'synthetic');"),
        "SYN_MilDB.db": database("CREATE TABLE MIL(id TEXT,pos REAL,data TEXT); INSERT INTO MIL VALUES('block',0,'" + mil + "');"),
        "1_StepInfo.db": database("CREATE TABLE T_Block(Pos REAL,BlockID TEXT); CREATE TABLE T_Step(Pos INTEGER,BlockID TEXT,MilID TEXT,StepSize INTEGER); INSERT INTO T_Block VALUES(0,'block'); INSERT INTO T_Step VALUES(0,'block','',1);"),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return buffer.getvalue()


class BundledReaderTests(unittest.TestCase):
    def test_bundled_reader_loads_decodes_and_keeps_unknown_closed(self):
        overlay = core._validated_public_upstream()
        contracts = core._require_source_contract()
        for width in ("A32", "A16"):
            with self.subTest(width=width), tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / "synthetic.gx3"
                body = synthetic_source(width)
                source.write_bytes(body)
                ir = upstream_contracts.build_source_neutral_ir(source, source_bytes=body, contracts=contracts, public_overlay=overlay)
                self.assertEqual(ir["producer"]["source_commit"], overlay.public_provenance["commit"])
                rows = ir["pous"][0]["records"]
                if width == "A32":
                    self.assertEqual(rows[0]["opcode"], "DPLSV")
                    self.assertEqual([r["operands"][0]["raw_token"] for r in rows], ["D12", "Y10", "Y14"])
                    self.assertEqual(ir["coverage"]["record"]["unknown"], 0)
                else:
                    self.assertEqual(ir["coverage"]["record"]["unknown"], 1)
                self.assertEqual(source.read_bytes(), body)
                self.assertNotIn("gx3_fx5_profile.decoder", sys.modules)


class SourceSemanticModelTests(unittest.TestCase):
    def decode(self, mil):
        return core._decode_mil_block(mil, [core.StepInfoRow(0, None, "block", None, 1)], 0)[0]

    def test_source_model_preserves_double_word_width(self):
        for marker in ("PLSV", "DSZR", "DRVA"):
            row = self.decode(positioning_mil(marker=marker))
            self.assertEqual(row.instruction, "D" + marker)
            expected = ("Y10", "Y14") if marker == "PLSV" else ("D24", "K3", "M40")
            self.assertEqual((row.operand, row.continuation_operands), ("D12", expected))
            for old, new in (("A32", "A16"), ("ct=a", "ct=p"), ("op=#", "op=0"), ("vt=nn", "vt=nx"), ("a=12", "a=-1"), ("s=#", "s=0"), ("Aea", "A16")):
                with self.subTest(marker=marker, mutation=(old, new)), self.assertRaises(core.ConversionError):
                    self.decode(positioning_mil(marker=marker).replace(old, new))

    def test_dx_exact_contact_context(self):
        for logic, opcode in (("a", "ANI"), ("o", "ORI")):
            mil = (f"V1:1:1:B:DX:ms{{el=[mc{{op=lct{{op=#:lt={logic}:ct=a:as=[as{{vt=Abl}}]}}:"
                   "as=[d{s=#:a=42:vt=nn}]}]}")
            row = self.decode(mil)
            self.assertEqual((row.instruction, row.operand), (opcode, "DX2A"))
            for old, new in (("ct=a", "ct=p"), ("op=#", "op=0"), ("vt=nn", "vt=nx"), ("a=42", "a=-1")):
                with self.assertRaises(core.ConversionError):
                    self.decode(mil.replace(old, new))

    def test_existing_indexed_f_route_is_not_shadowed_by_scalar(self):
        for opcode in ("LD", "OR", "OUT"):
            operation = "cl{op=#:ct=a:as=[as{vt=Abl}]}" if opcode == "OUT" else "lct{op=#:lt=" + {"LD": "l", "OR": "o"}[opcode] + ":ct=a:as=[as{vt=Abl}]}"
            header = "OUT" if opcode == "OUT" else "A"
            mil = f"V1:1:1:{header}:F:Zs:ms{{el=[mc{{op={operation}:as=[M{{b=d{{s=#:a=12:vt=nn}}:m=d{{s=#:a=3:vt=nn}}}}]}}]}}"
            self.assertEqual((self.decode(mil).instruction, self.decode(mil).operand), (opcode, "F12Z3"))
            for old, new in (("vt=nn", "vt=nx"), ("s=#", "s=0"), ("b=d", "b=c"), ("m=d", "m=c"), ("a=12", "a=-1"), ("a=3", "a=-1"), ("op=#", "op=0"), ("ct=a", "ct=p"), ("a=12", "extra=1:a=12")):
                with self.subTest(opcode=opcode, mutation=(old, new)), self.assertRaises(core.ConversionError):
                    self.decode(mil.replace(old, new))
            result = core._mil_format_operand_in_context(opcode, ("Abl",), ["F", "Zs"], 0, ["F", "Zs"], ("k_device", (12, 3)))
            self.assertEqual(result, ("F12Z3", 2))
            for values in ((-1, 3), (12, -1)):
                with self.assertRaises(core.ConversionError):
                    core._mil_format_operand_in_context(opcode, ("Abl",), ["F", "Zs"], 0, ["F", "Zs"], ("k_device", values))
        self.assertEqual(core._mil_format_operand_in_context("LD", ("Abl",), ["F"], 0, ["F"], ("scalar", (12,))), ("F12", 1))
        with self.assertRaises(core.ConversionError):
            core._mil_format_operand(["DX"], ("scalar", (42,)))
