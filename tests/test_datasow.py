"""datasow 端到端测试：只读 samples/，数据写到临时目录。"""
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "datasow", *args],
                          cwd=ROOT, capture_output=True, text=True)


def gen(schema_path, out_dir, *extra):
    result = run_cli("gen", "--schema", str(schema_path),
                     "--out", str(out_dir), *extra)
    assert result.returncode == 0, result.stderr
    return result


def validate(schema_path, data_dir, report_path):
    return run_cli("validate", "--schema", str(schema_path),
                   "--data", str(data_dir), "--report", str(report_path))


def load_expected(name):
    lines = []
    with open(SAMPLES / "expected" / f"{name}.txt", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            id_, expected, tolerance = raw.split(" ")
            lines.append((id_, expected, tolerance))
    return lines


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class TestSamples(unittest.TestCase):
    """三个样例：report 的检查项与 samples/expected/*.txt 逐行一致且全过。"""

    def check_sample(self, name):
        schema = SAMPLES / "schema" / f"{name}.json"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "data"
            gen(schema, out)
            report_path = Path(tmp) / "report.json"
            result = validate(schema, out, report_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report["summary"]["fail"], 0)
        checks = [c for t in report["tables"] for c in t["checks"]]
        expected = load_expected(name)
        self.assertEqual([c["id"] for c in checks],
                         [e[0] for e in expected])
        for (id_, exp, tol), check in zip(expected, checks):
            self.assertEqual(check["status"], "pass",
                             f"{id_}: {check}")
            if exp == "pass":
                self.assertEqual(check["expected"], "pass", id_)
            elif id_.startswith(("rows:", "unique:")):
                self.assertEqual(check["expected"], int(exp), id_)
            elif id_.startswith("columns:"):
                self.assertEqual(check["expected"], exp, id_)
            else:
                self.assertEqual("%.5f" % check["expected"], exp, id_)
            if tol == "-":
                self.assertIsNone(check["tolerance"], id_)
            else:
                self.assertAlmostEqual(check["tolerance"], float(tol),
                                       places=9, msg=id_)

    def test_sample_01_orders(self):
        self.check_sample("01_orders")

    def test_sample_02_catalog(self):
        self.check_sample("02_catalog")

    def test_sample_03_sensor(self):
        self.check_sample("03_sensor")


class TestReproducibility(unittest.TestCase):
    SCHEMA = SAMPLES / "schema" / "01_orders.json"

    def test_same_seed_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            out1, out2 = Path(tmp) / "a", Path(tmp) / "b"
            gen(self.SCHEMA, out1)
            gen(self.SCHEMA, out2)
            for name in ("customers", "orders"):
                self.assertEqual(sha256(out1 / f"{name}.jsonl"),
                                 sha256(out2 / f"{name}.jsonl"), name)

    def test_seed_override_and_difference(self):
        with tempfile.TemporaryDirectory() as tmp:
            out1, out2 = Path(tmp) / "a", Path(tmp) / "b"
            gen(self.SCHEMA, out1, "--seed", "7")
            gen(self.SCHEMA, out2, "--seed", "8")
            self.assertNotEqual(sha256(out1 / "orders.jsonl"),
                                sha256(out2 / "orders.jsonl"))
            # 换了种子判定仍然全过
            report = Path(tmp) / "report.json"
            result = validate(self.SCHEMA, out2, report)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_seed_is_error(self):
        schema = json.loads(self.SCHEMA.read_text(encoding="utf-8"))
        del schema["seed"]
        with tempfile.TemporaryDirectory() as tmp:
            no_seed = Path(tmp) / "noseed.json"
            no_seed.write_text(json.dumps(schema), encoding="utf-8")
            result = run_cli("gen", "--schema", str(no_seed),
                             "--out", str(Path(tmp) / "out"))
            self.assertEqual(result.returncode, 2)
            # 命令行 --seed 可以补上
            result = run_cli("gen", "--schema", str(no_seed),
                             "--out", str(Path(tmp) / "out2"),
                             "--seed", "1")
            self.assertEqual(result.returncode, 0, result.stderr)


class TestOutputFormat(unittest.TestCase):
    SCHEMA = SAMPLES / "schema" / "01_orders.json"

    def test_jsonl_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "data"
            gen(self.SCHEMA, out, "--batch-size", "7")
            schema = json.loads(self.SCHEMA.read_text(encoding="utf-8"))
            for table in schema["tables"]:
                path = out / f"{table['name']}.jsonl"
                raw = path.read_bytes()
                self.assertTrue(raw.endswith(b"\n"), "末尾必须有换行")
                self.assertNotIn(b"\r", raw)
                names = [c["name"] for c in table["columns"]]
                lines = raw.decode("utf-8").splitlines()
                self.assertEqual(len(lines),
                                 math.ceil(table["rows"] * 1.0))
                for line in lines[:50] + lines[-50:]:
                    obj = json.loads(line)
                    self.assertEqual(list(obj.keys()), names)
                # 类型抽查
                first = json.loads(lines[0])
                for col in table["columns"]:
                    value = first[col["name"]]
                    if value is None:
                        continue
                    if col["type"] == "int":
                        self.assertIsInstance(value, int)
                        self.assertNotIsInstance(value, bool)
                    elif col["type"] == "float":
                        self.assertIsInstance(value, float)
                    elif col["type"] == "bool":
                        self.assertIsInstance(value, bool)
                    else:
                        self.assertIsInstance(value, str)

    def test_float_precision_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "data"
            gen(self.SCHEMA, out)
            with open(out / "orders.jsonl", encoding="utf-8") as fh:
                for _ in range(200):
                    obj = json.loads(fh.readline())
                    text = repr(obj["amount"])
                    decimals = text.split(".")[1] if "." in text else ""
                    self.assertLessEqual(len(decimals), 2)

    def test_rows_scale(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "data"
            gen(self.SCHEMA, out, "--rows-scale", "2.5")
            schema = json.loads(self.SCHEMA.read_text(encoding="utf-8"))
            for table in schema["tables"]:
                with open(out / f"{table['name']}.jsonl",
                          encoding="utf-8") as fh:
                    count = sum(1 for _ in fh)
                self.assertEqual(count, math.ceil(table["rows"] * 2.5))
            report = Path(tmp) / "report.json"
            result = validate(self.SCHEMA, out, report)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["rows_scale"], 2.5)

    def test_report_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "data"
            gen(self.SCHEMA, out)
            report_path = Path(tmp) / "report.json"
            validate(self.SCHEMA, out, report_path)
            raw = report_path.read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            report = json.loads(raw)
            for key in ("schema", "seed", "rows_scale", "tables", "summary"):
                self.assertIn(key, report)
            table = report["tables"][0]
            for key in ("name", "rows", "columns", "checks"):
                self.assertIn(key, table)
            col = table["columns"][0]
            for key in ("name", "type", "null_count", "unique_count",
                        "buckets"):
                self.assertIn(key, col)
            bucket = col["buckets"][0]
            for key in ("key", "count", "ratio"):
                self.assertIn(key, bucket)
            check = table["checks"][0]
            for key in ("id", "kind", "status", "expected", "measured",
                        "tolerance", "detail"):
                self.assertIn(key, check)
            for key in ("total", "pass", "fail"):
                self.assertIn(key, report["summary"])


class TestErrors(unittest.TestCase):
    def test_bad_schema_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            result = run_cli("gen", "--schema", str(bad),
                             "--out", str(Path(tmp) / "out"))
            self.assertEqual(result.returncode, 2)

    def test_missing_data_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = validate(SAMPLES / "schema" / "01_orders.json",
                              Path(tmp) / "nonexistent",
                              Path(tmp) / "report.json")
            self.assertEqual(result.returncode, 2)

    def test_validate_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "data"
            gen(SAMPLES / "schema" / "01_orders.json", out)
            # 篡改：把 customers 的 region 全部改成一个值
            path = out / "customers.jsonl"
            rows = [json.loads(line) for line in
                    path.read_text(encoding="utf-8").splitlines()]
            for row in rows:
                row["region"] = "north"
            path.write_text("".join(json.dumps(r) + "\n" for r in rows),
                            encoding="utf-8")
            report = Path(tmp) / "report.json"
            result = validate(SAMPLES / "schema" / "01_orders.json",
                              out, report)
            self.assertEqual(result.returncode, 1)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertGreater(data["summary"]["fail"], 0)


if __name__ == "__main__":
    unittest.main()
