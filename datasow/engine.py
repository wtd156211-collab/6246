"""数据生成引擎：按 schema 逐行生成、边生成边落盘。

可复现性：每张表的 RNG 由 sha256(seed:table) 派生，列按声明顺序消耗
随机流；不依赖系统时间、进程号、os.urandom 与集合迭代顺序。
内存：只保留被引用列取值、唯一性集合与当前批次的输出行，随行数线性
之外不累积整表数据。
"""
from __future__ import annotations

import bisect
import hashlib
import itertools
import json
import math
import os
import random
from array import array

from .schema import check_unique_space

MAX_RETRIES = 1000


class GenerationError(RuntimeError):
    pass


def _derive_seed(seed, table_name):
    digest = hashlib.sha256(f"{seed}:{table_name}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _cum_weights(weights):
    # 用 array('d') 而非 list：百万级取值时内存不随行数膨胀
    out = array("d")
    total = 0.0
    for w in weights:
        total += w
        out.append(total)
    return out


def _sorted_ref_values(ref_values):
    """被引用取值的升序序列；已升序（如 serial）时直接复用，不复制。"""
    if all(b >= a for a, b in zip(ref_values,
                                  itertools.islice(ref_values, 1, None))):
        return ref_values
    return array("q", sorted(ref_values))


class _ColumnGen:
    """单列取值器：sample() 产生一个非空值（空值由行级逻辑决定）。"""

    __slots__ = ("col", "sample", "resamplable", "is_serial", "base_name",
                 "self_ref_list")

    def __init__(self, col, sample, resamplable=True, is_serial=False,
                 base_name=None, self_ref_list=None):
        self.col = col
        self.sample = sample
        self.resamplable = resamplable
        self.is_serial = is_serial
        self.base_name = base_name
        self.self_ref_list = self_ref_list


class TableGenerator:
    def __init__(self, schema, table, seed, ref_store):
        self.schema = schema
        self.table = table
        self.ref_store = ref_store
        self.rng = random.Random(_derive_seed(seed, table.name))
        self.col_gens = []
        self.self_ref_lists = {}   # 被自引用列名 -> 已生成取值列表
        self.offset_by_base = {}   # 基准列名 -> [offset 列生成器]
        self.unique_sets = {}      # 非 serial unique 列 -> set
        self.uk_sets = [set() for _ in table.unique_keys]
        self._build()

    # ---- 构建 ----

    def _build(self):
        table = self.table
        for fk in table.foreign_keys:
            if fk.ref_table == table.name and len(fk.ref_columns) == 1:
                ref_col = table.column(fk.ref_columns[0])
                store = array("q") if ref_col.type == "int" else []
                self.self_ref_lists.setdefault(fk.ref_columns[0], store)
        for col in table.columns:
            gen = self._make_col_gen(col)
            self.col_gens.append(gen)
            if gen.base_name is not None:
                self.offset_by_base.setdefault(gen.base_name, []).append(gen)
            if col.unique and col.dist_kind != "serial":
                self.unique_sets[col.name] = set()
                check_unique_space(self.schema, table, col, table.rows)

    def _make_col_gen(self, col):
        rng = self.rng
        kind = col.dist_kind
        dist = col.distribution or {}
        if kind == "serial":
            state = {"next": dist.get("start", 0)}
            step = dist.get("step", 1)

            def sample_serial(state=state, step=step):
                v = state["next"]
                state["next"] = v + step
                return v

            return _ColumnGen(col, sample_serial, resamplable=False,
                              is_serial=True)
        if kind == "categorical":
            values = dist["values"]
            cum = _cum_weights(dist["weights"])
            total = cum[-1]

            def sample_cat(values=values, cum=cum, total=total):
                return values[bisect.bisect_left(cum, rng.random() * total)]

            return _ColumnGen(col, sample_cat)
        if kind == "zipf":
            n = col.max - col.min + 1
            exponent = dist["exponent"]
            cum = _cum_weights([(i + 1) ** (-exponent) for i in range(n)])
            total = cum[-1]
            lo = col.min

            def sample_zipf(lo=lo, cum=cum, total=total):
                return lo + bisect.bisect_left(cum, rng.random() * total)

            return _ColumnGen(col, sample_zipf)
        if kind == "offset":
            base_name = dist["column"]
            omin, omax = dist["min"], dist["max"]

            def sample_offset(base_name=base_name, omin=omin, omax=omax):
                base = self.row[base_name]
                if base is None:
                    return None
                return base + rng.randint(omin, omax)

            return _ColumnGen(col, sample_offset, base_name=base_name)
        if kind in ("ref_uniform", "ref_zipf"):
            fk = self.schema.fk_for_ref_column(self.table, col.name)
            if fk.ref_table == self.table.name:
                live = self.self_ref_lists[fk.ref_columns[0]]

                def sample_self(live=live):
                    if not live:
                        return None
                    return live[rng.randrange(len(live))]

                return _ColumnGen(col, sample_self, self_ref_list=live)
            ref_values = self.ref_store[(fk.ref_table, fk.ref_columns[0])]
            if not ref_values:
                raise GenerationError(
                    f"列 {self.table.name}.{col.name}: 被引用集合为空")
            if kind == "ref_uniform":
                n = len(ref_values)

                def sample_ref_uniform(ref_values=ref_values, n=n):
                    return ref_values[rng.randrange(n)]

                return _ColumnGen(col, sample_ref_uniform)
            sorted_values = _sorted_ref_values(ref_values)
            exponent = dist["exponent"]
            cum = _cum_weights([(i + 1) ** (-exponent)
                                for i in range(len(sorted_values))])
            total = cum[-1]

            def sample_ref_zipf(sorted_values=sorted_values, cum=cum,
                                total=total):
                idx = bisect.bisect_left(cum, rng.random() * total)
                return sorted_values[idx]

            return _ColumnGen(col, sample_ref_zipf)
        # uniform（含未声明分布的类型默认）
        if col.type == "int":
            lo, hi = col.min, col.max

            def sample_int(lo=lo, hi=hi):
                return rng.randint(lo, hi)

            return _ColumnGen(col, sample_int)
        if col.type == "float":
            lo, hi = float(col.min), float(col.max)
            precision = col.precision

            def sample_float(lo=lo, hi=hi, precision=precision):
                return round(lo + rng.random() * (hi - lo), precision)

            return _ColumnGen(col, sample_float)
        if col.type == "bool":
            def sample_bool():
                return rng.random() < 0.5

            return _ColumnGen(col, sample_bool)
        # string
        min_len, max_len = col.min_len, col.max_len
        charset = col.charset

        def sample_string(min_len=min_len, max_len=max_len, charset=charset):
            return "".join(rng.choices(charset,
                                       k=rng.randint(min_len, max_len)))

        return _ColumnGen(col, sample_string)

    # ---- 行生成与约束 ----

    row = None  # 当前行 dict，供 offset 取样器读取

    def _gen_value(self, gen):
        col = gen.col
        if col.null_rate > 0.0 and self.rng.random() < col.null_rate:
            return None
        return gen.sample()

    def _new_row(self):
        row = {}
        self.row = row
        for gen in self.col_gens:
            row[gen.col.name] = self._gen_value(gen)
        return row

    def _resample(self, name):
        gen = self.gen_by_name[name]
        self.row[name] = gen.sample()
        for dep in self.offset_by_base.get(name, ()):
            self.row[dep.col.name] = dep.sample()

    def _violation(self, row):
        """返回需要重抽的列名列表；无违例返回 None。"""
        for name, seen in self.unique_sets.items():
            if row[name] in seen:
                return [name]
        for uk, seen in zip(self.table.unique_keys, self.uk_sets):
            if tuple(row[c] for c in uk) in seen:
                targets = [c for c in uk if self.gen_by_name[c].resamplable]
                if not targets:
                    raise GenerationError(
                        f"表 {self.table.name}: 唯一键 {uk} 冲突且列均不可重抽")
                return targets
        for ck in self.table.checks:
            left, right = row[ck.left], row[ck.right]
            if left is None or right is None:
                continue
            if not _compare(ck.kind, left, right):
                targets = [c for c in (ck.left, ck.right)
                           if self.gen_by_name[c].resamplable]
                if not targets:
                    raise GenerationError(
                        f"表 {self.table.name}: 约束 {ck.left}{ck.op}{ck.right} "
                        "涉及列均不可重抽")
                return targets
        return None

    def _check_row(self, row):
        attempts = 0
        while True:
            targets = self._violation(row)
            if targets is None:
                return
            attempts += 1
            if attempts > MAX_RETRIES:
                raise GenerationError(
                    f"表 {self.table.name}: 单行重抽超过 {MAX_RETRIES} 次")
            for name in targets:
                self._resample(name)

    def _record(self, row):
        for name, seen in self.unique_sets.items():
            seen.add(row[name])
        for uk, seen in zip(self.table.unique_keys, self.uk_sets):
            seen.add(tuple(row[c] for c in uk))
        for col_name, live in self.self_ref_lists.items():
            live.append(row[col_name])

    # ---- 序列化 ----

    def _serializer(self):
        parts = []
        for col in self.table.columns:
            prefix = json.dumps(col.name, ensure_ascii=False) + ":"
            if col.type == "int":
                fmt = str
            elif col.type == "float":
                p = col.precision

                def fmt(v, p=p):
                    return f"{v:.{p}f}"
            elif col.type == "bool":
                def fmt(v):
                    return "true" if v else "false"
            else:
                def fmt(v):
                    return json.dumps(v, ensure_ascii=False)
            parts.append((col.name, prefix, fmt))

        def serialize(row):
            out = []
            for name, prefix, fmt in parts:
                v = row[name]
                out.append(prefix + ("null" if v is None else fmt(v)))
            return "{" + ",".join(out) + "}\n"

        return serialize

    # ---- 主流程 ----

    def run(self, n_rows, path, batch_size):
        self.gen_by_name = {g.col.name: g for g in self.col_gens}
        for col in self.table.columns:
            if col.unique and col.dist_kind != "serial":
                check_unique_space(self.schema, self.table, col, n_rows)
        serialize = self._serializer()
        buf = []
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for _ in range(n_rows):
                row = self._new_row()
                self._check_row(row)
                self._record(row)
                buf.append(serialize(row))
                if len(buf) >= batch_size:
                    fh.write("".join(buf))
                    fh.flush()
                    buf.clear()
            if buf:
                fh.write("".join(buf))
                fh.flush()
        # 供后续表引用的取值（按落盘顺序）
        for fk_table, fk_col in self._referenced_columns():
            values = self._column_values(fk_col)
            self.ref_store[(self.table.name, fk_col)] = values

    def _referenced_columns(self):
        seen = []
        for table in self.schema.tables:
            for fk in table.foreign_keys:
                if fk.ref_table == self.table.name:
                    for cn in fk.ref_columns:
                        if cn not in seen:
                            seen.append(cn)
        return [(self.table.name, cn) for cn in seen]

    def _column_values(self, col_name):
        # 被引用列的取值在生成过程中已逐行累积（见 generate()）。
        if col_name in self.self_ref_lists:
            return self.self_ref_lists[col_name]
        return self._collected[col_name]


def _compare(kind, left, right):
    if kind == "lt":
        return left < right
    if kind == "lte":
        return left <= right
    if kind == "gt":
        return left > right
    if kind == "gte":
        return left >= right
    return left != right


def generate(schema, out_dir, seed, rows_scale=1.0, batch_size=2048):
    os.makedirs(out_dir, exist_ok=True)
    ref_store = {}
    for table in schema.tables:
        n_rows = math.ceil(table.rows * rows_scale)
        gen = TableGenerator(schema, table, seed, ref_store)
        gen._collected = {}
        for _, cn in gen._referenced_columns():
            if cn in gen.self_ref_lists:
                continue
            col = table.column(cn)
            gen._collected[cn] = array("q") if col.type == "int" else []
        # 生成时需要记录被后续表引用的列：包一层 record
        if gen._collected:
            orig_record = gen._record
            collected = gen._collected

            def record(row, orig_record=orig_record, collected=collected):
                orig_record(row)
                for cn, lst in collected.items():
                    lst.append(row[cn])

            gen._record = record
        gen.run(n_rows, os.path.join(out_dir, table.name + ".jsonl"),
                batch_size)
    return [t.name for t in schema.tables]
