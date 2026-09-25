"""校验器：只读数据目录，统计、判定并产出 report.json。

两遍读文件：先收集被引用列的取值集合（供外键判定与 ref_* 分桶值域），
再做全量统计。除被引用集合与当前表的列统计外不累积数据。
"""
from __future__ import annotations

import json
import math
import os

from .dist import (expected_ratios, is_bucket_judged, make_bucket_spec)
from .engine import _compare


class DataError(Exception):
    """数据目录不可读或内容非法（退出码 2）。"""


def _type_ok(ctype, value):
    if ctype == "bool":
        return isinstance(value, bool)
    if ctype == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if ctype == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str)


class _ColumnStats:
    __slots__ = ("col", "null_count", "uniques", "spec", "bucket_counts",
                 "observed_lo", "observed_hi", "ref_sorted")

    def __init__(self, col):
        self.col = col
        self.null_count = 0
        self.uniques = set()
        self.spec = None
        self.bucket_counts = None
        self.observed_lo = None
        self.observed_hi = None
        self.ref_sorted = None


class _TableStats:
    def __init__(self, table):
        self.table = table
        self.rows = 0
        self.columns = [_ColumnStats(c) for c in table.columns]
        self.signature = None
        self.structure_error = None
        self.uk_sizes = [0] * len(table.unique_keys)
        self.uk_null_rows = 0
        self.ineq_violations = [0] * len(table.checks)
        self.ineq_evaluated = [0] * len(table.checks)
        self.fk_missing = [0] * len(table.foreign_keys)
        self.fk_nulls = [0] * len(table.foreign_keys)


def _read_lines(path):
    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError as exc:
        raise DataError(f"无法读取数据文件 {path}: {exc}") from exc
    with fh:
        for lineno, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                yield lineno, json.loads(line)
            except json.JSONDecodeError as exc:
                raise DataError(f"{path}:{lineno}: 非法 JSON 行: {exc}") from exc


def _referenced_by(schema):
    """表名 -> 被某外键引用的列名列表。"""
    out = {}
    for table in schema.tables:
        for fk in table.foreign_keys:
            slot = out.setdefault(fk.ref_table, [])
            for cn in fk.ref_columns:
                if cn not in slot:
                    slot.append(cn)
    return out


def _collect_ref_values(schema, data_dir):
    """第一遍：收集每个被引用 (表, 列) 的取值集合与升序列表。"""
    wanted = _referenced_by(schema)
    store = {}
    for table in schema.tables:
        cols = wanted.get(table.name)
        if not cols:
            continue
        path = os.path.join(data_dir, table.name + ".jsonl")
        values = {cn: set() for cn in cols}
        for _, obj in _read_lines(path):
            if not isinstance(obj, dict):
                raise DataError(f"{path}: 行必须是 JSON 对象")
            for cn in cols:
                v = obj.get(cn)
                if v is not None:
                    values[cn].add(v)
        for cn, vals in values.items():
            store[(table.name, cn)] = (vals, sorted(vals))
    return store


def _prepare_buckets(stats, schema, ref_store):
    """为每列建分桶；serial 等无声明值域的列等观测后再建。"""
    table = stats.table
    for cs in stats.columns:
        col = cs.col
        ref_sorted = None
        if col.dist_kind in ("ref_uniform", "ref_zipf"):
            fk = schema.fk_for_ref_column(table, col.name)
            key = (fk.ref_table, fk.ref_columns[0])
            if key not in ref_store:
                raise DataError(
                    f"列 {table.name}.{col.name}: 缺少被引用数据 {key}")
            ref_sorted = ref_store[key][1]
            if not ref_sorted:
                raise DataError(f"列 {table.name}.{col.name}: 被引用集合为空")
        if col.type == "int" and col.min is None \
                and col.dist_kind in (None, "serial"):
            continue  # serial 等观测值域列：扫描结束后建桶
        cs.spec = make_bucket_spec(col, table, schema, ref_sorted=ref_sorted)
        cs.bucket_counts = [0] * cs.spec.k
        cs.ref_sorted = ref_sorted  # 供期望占比计算


def _scan_table(stats, schema, data_dir, ref_store):
    table = stats.table
    path = os.path.join(data_dir, table.name + ".jsonl")
    names = [c.name for c in table.columns]
    types = [c.type for c in table.columns]
    col_stats = stats.columns
    uk_sets = [set() for _ in table.unique_keys]
    fk_defs = table.foreign_keys
    fk_ref_sets = []
    for fk in fk_defs:
        key = (fk.ref_table, fk.ref_columns[0])
        if key not in ref_store:
            raise DataError(f"表 {table.name}: 缺少被引用数据 {key}")
        fk_ref_sets.append(ref_store[key][0])
    expected_sig = ",".join(f"{c.name}:{c.type}" for c in table.columns)

    for lineno, obj in _read_lines(path):
        if not isinstance(obj, dict):
            raise DataError(f"{path}:{lineno}: 行必须是 JSON 对象")
        stats.rows += 1
        keys = list(obj.keys())
        if keys != names and stats.structure_error is None:
            stats.structure_error = f"第 {lineno} 行列顺序/集合不符"
            if stats.signature is None:
                stats.signature = ",".join(keys)
        for i, col in enumerate(table.columns):
            v = obj.get(col.name)
            cs = col_stats[i]
            if v is None:
                cs.null_count += 1
                if col.not_null and stats.structure_error is None:
                    stats.structure_error = f"第 {lineno} 行 {col.name} 空值"
                continue
            if not _type_ok(types[i], v) and stats.structure_error is None:
                stats.structure_error = (
                    f"第 {lineno} 行 {col.name} 类型应为 {types[i]}")
            cs.uniques.add(v)
            if (col.type == "int" and col.min is None
                    and col.dist_kind in (None, "serial")):
                if cs.observed_lo is None or v < cs.observed_lo:
                    cs.observed_lo = v
                if cs.observed_hi is None or v > cs.observed_hi:
                    cs.observed_hi = v
            if cs.spec is not None:
                idx = cs.spec.assign(len(v) if cs.spec.kind == "length"
                                     else v)
                if idx >= len(cs.bucket_counts):
                    cs.bucket_counts.extend(
                        [0] * (idx + 1 - len(cs.bucket_counts)))
                cs.bucket_counts[idx] += 1
        for ui, uk in enumerate(table.unique_keys):
            tup = tuple(obj.get(c) for c in uk)
            if any(v is None for v in tup):
                stats.uk_null_rows += 1
            else:
                uk_sets[ui].add(tup)
        for ci, ck in enumerate(table.checks):
            left, right = obj.get(ck.left), obj.get(ck.right)
            if left is None or right is None:
                continue
            stats.ineq_evaluated[ci] += 1
            if not _compare(ck.kind, left, right):
                stats.ineq_violations[ci] += 1
        for fi, fk in enumerate(fk_defs):
            v = obj.get(fk.columns[0])
            if v is None:
                if not fk.null_ok:
                    stats.fk_nulls[fi] += 1
            elif v not in fk_ref_sets[fi]:
                stats.fk_missing[fi] += 1

    stats.uk_sizes = [len(s) for s in uk_sets]
    if stats.signature is None:
        stats.signature = expected_sig
    # serial 等观测值域列现在建桶
    for cs in col_stats:
        if (cs.spec is None and cs.col.type == "int"
                and cs.col.min is None
                and cs.col.dist_kind in (None, "serial")):
            if cs.observed_lo is not None:
                cs.spec = make_bucket_spec(
                    cs.col, table, schema,
                    observed=(cs.observed_lo, cs.observed_hi))
                cs.bucket_counts = [0] * cs.spec.k
    return expected_sig


def _rescan_observed_buckets(stats, data_dir):
    """观测值域列（serial）需要第二遍填充桶计数。"""
    targets = [cs for cs in stats.columns
               if cs.spec is not None and cs.bucket_counts is not None
               and cs.col.type == "int" and cs.col.min is None
               and cs.col.dist_kind in (None, "serial")]
    if not targets:
        return
    path = os.path.join(data_dir, stats.table.name + ".jsonl")
    names = [cs.col.name for cs in targets]
    for _, obj in _read_lines(path):
        for cs in targets:
            v = obj.get(cs.col.name)
            if v is not None:
                cs.bucket_counts[cs.spec.assign(v)] += 1


def _round(value, digits):
    return round(value + 0.0, digits)


def _build_checks(stats, schema, ref_store, scale, expected_sig):
    table = stats.table
    checks = []
    widen = max(1.0, math.sqrt(1.0 / scale)) if scale > 0 else 1.0
    rows = stats.rows

    def add(id_, kind, status, expected, measured, tolerance, detail=""):
        checks.append({
            "id": id_, "kind": kind, "status": status,
            "expected": expected, "measured": measured,
            "tolerance": tolerance, "detail": detail,
        })

    # rows
    expected_rows = math.ceil(table.rows * scale)
    add(f"rows:{table.name}", "rows",
        "pass" if rows == expected_rows else "fail",
        expected_rows, rows, None)
    # columns
    actual_sig = stats.signature or expected_sig
    ok = stats.structure_error is None and actual_sig == expected_sig
    add(f"columns:{table.name}", "columns",
        "pass" if ok else "fail", expected_sig, actual_sig, None,
        stats.structure_error or "")
    # nullratio
    for cs in stats.columns:
        col = cs.col
        tol = _round(col.tolerance * widen, 6)
        measured = _round(cs.null_count / rows, 6) if rows else 0.0
        expected = _round(col.null_rate, 5)
        status = "pass" if abs(measured - col.null_rate) <= tol else "fail"
        add(f"nullratio:{table.name}.{col.name}", "nullratio", status,
            expected, measured, tol)
    # unique
    for cs in stats.columns:
        col = cs.col
        if not col.unique:
            continue
        uniq = len(cs.uniques)
        status = "pass" if (uniq == rows and cs.null_count == 0) else "fail"
        detail = "" if status == "pass" else (
            f"unique_count={uniq} null_count={cs.null_count}")
        add(f"unique:{table.name}.{col.name}", "unique", status,
            rows, uniq, None, detail)
    # uk
    for ui, uk in enumerate(table.unique_keys):
        size = stats.uk_sizes[ui]
        ok = size == rows and stats.uk_null_rows == 0
        detail = "" if ok else (
            f"distinct={size} null_rows={stats.uk_null_rows}")
        add(f"uk:{table.name}." + "+".join(uk), "uk",
            "pass" if ok else "fail", "pass",
            "pass" if ok else "fail", None, detail)
    # fk
    for fi, fk in enumerate(table.foreign_keys):
        missing = stats.fk_missing[fi]
        nulls = stats.fk_nulls[fi]
        ok = missing == 0 and nulls == 0
        detail = "" if ok else f"missing={missing} nulls={nulls}"
        add(f"fk:{table.name}." + "+".join(fk.columns)
            + "->" + fk.ref_table + "." + "+".join(fk.ref_columns), "fk",
            "pass" if ok else "fail", "pass",
            "pass" if ok else "fail", None, detail)
    # ineq
    for ci, ck in enumerate(table.checks):
        viol = stats.ineq_violations[ci]
        ok = viol == 0
        detail = ("" if ok else
                  f"violations={viol} evaluated={stats.ineq_evaluated[ci]}")
        add(f"ineq:{table.name}.{ck.left}{ck.op}{ck.right}", "ineq",
            "pass" if ok else "fail", "pass",
            "pass" if ok else "fail", None, detail)
    # bucket
    for cs in stats.columns:
        col = cs.col
        if not is_bucket_judged(col, table, schema):
            continue
        expected_list = expected_ratios(col, table, schema, cs.spec,
                                        ref_sorted=cs.ref_sorted)
        tol = _round(col.tolerance * widen, 6)
        for i, key in enumerate(cs.spec.keys):
            expected = expected_list[i] if i < len(expected_list) else 0.0
            count = cs.bucket_counts[i] if i < len(cs.bucket_counts) else 0
            measured = count / rows if rows else 0.0
            ok = abs(measured - expected) <= col.tolerance * widen
            if expected == 0.0 and count != 0:
                ok = False
            add(f"bucket:{table.name}.{col.name}#{key}", "bucket",
                "pass" if ok else "fail",
                _round(expected, 5), _round(measured, 6), tol)
    return checks


def validate(schema, data_dir, schema_path):
    """返回 report dict。数据不可读抛 DataError。"""
    ref_store = _collect_ref_values(schema, data_dir)
    all_stats = []
    expected_sigs = {}
    for table in schema.tables:
        stats = _TableStats(table)
        _prepare_buckets(stats, schema, ref_store)
        sig = _scan_table(stats, schema, data_dir, ref_store)
        _rescan_observed_buckets(stats, data_dir)
        expected_sigs[table.name] = sig
        all_stats.append(stats)
    first = all_stats[0]
    scale = (first.rows / first.table.rows) if first.table.rows else 1.0
    tables_out = []
    total = passed = failed = 0
    for stats in all_stats:
        table = stats.table
        checks = _build_checks(stats, schema, ref_store, scale,
                               expected_sigs[table.name])
        columns_out = []
        for cs in stats.columns:
            col = cs.col
            buckets = []
            if cs.spec is not None and cs.bucket_counts is not None:
                for i, key in enumerate(cs.spec.keys):
                    count = (cs.bucket_counts[i]
                             if i < len(cs.bucket_counts) else 0)
                    buckets.append({
                        "key": key,
                        "count": count,
                        "ratio": _round(count / stats.rows, 6)
                        if stats.rows else 0.0,
                    })
            columns_out.append({
                "name": col.name,
                "type": col.type,
                "null_count": cs.null_count,
                "unique_count": len(cs.uniques),
                "buckets": buckets,
            })
            cs.uniques = None  # 释放内存
        for ck in checks:
            total += 1
            if ck["status"] == "pass":
                passed += 1
            else:
                failed += 1
        tables_out.append({
            "name": table.name,
            "rows": stats.rows,
            "columns": columns_out,
            "checks": checks,
        })
    return {
        "schema": schema_path,
        "seed": schema.seed,
        "rows_scale": _round(scale, 6),
        "tables": tables_out,
        "summary": {"total": total, "pass": passed, "fail": failed},
    }


def write_report(report, path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
