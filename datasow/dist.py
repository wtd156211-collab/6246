"""分桶、期望占比与分布的解析计算。

口径见 README 第 2 节与 samples/expected/checks.md：
- int/长度域等宽 K 桶，p_k = lo + floor(k*N/K)，左闭右开、末桶右闭；
- float 等宽 K 桶，边界 lo + (hi-lo)*k/K；
- 期望占比分母为总行数（含空值），非空桶乘 (1 - null_rate)。
"""
from __future__ import annotations

import bisect
import json


def fmt_float(x):
    """桶边界小数：最多 4 位，去掉尾零。"""
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    if s in ("", "-0"):
        s = "0"
    return s


def int_boundaries(lo, hi, k):
    n = hi - lo + 1
    return [lo + (i * n) // k for i in range(k + 1)]


def float_boundaries(lo, hi, k):
    return [lo + (hi - lo) * i / k for i in range(k + 1)]


def range_keys(boundaries, hi, prefix="", float_fmt=False):
    fmt = fmt_float if float_fmt else str
    keys = []
    last = len(boundaries) - 2
    for i in range(len(boundaries) - 1):
        a = fmt(boundaries[i])
        if i == last:
            keys.append(f"{prefix}[{a},{fmt(hi)}]")
        else:
            keys.append(f"{prefix}[{a},{fmt(boundaries[i + 1])})")
    return keys


def categorical_key(value):
    return "=" + json.dumps(value, ensure_ascii=False)


def assign_range(boundaries, value, k):
    """左闭右开、末桶右闭；越界值压入端桶。"""
    idx = bisect.bisect_right(boundaries, value) - 1
    if idx < 0:
        return 0
    if idx >= k:
        return k - 1
    return idx


class BucketSpec:
    """一列的分桶定义：键列表 + 赋值函数。"""

    __slots__ = ("kind", "keys", "boundaries", "values", "value_index", "k")

    def __init__(self, kind, keys, boundaries=None, values=None):
        self.kind = kind
        self.keys = keys
        self.boundaries = boundaries
        self.values = values
        self.k = len(keys)
        self.value_index = None
        if values is not None:
            self.value_index = {v: i for i, v in enumerate(values)}

    def assign(self, value):
        if self.kind == "categorical":
            idx = self.value_index.get(value)
            if idx is None:
                # 声明外取值：动态追加一桶（期望占比 0，必判 fail）
                idx = len(self.keys)
                self.value_index[value] = idx
                self.keys.append(categorical_key(value))
                self.k += 1
            return idx
        return assign_range(self.boundaries, value, self.k)


def column_domain(col, table, schema, ref_sorted=None, observed=None):
    """返回值域：(kind, lo, hi) 或 ("categorical", values)。

    kind ∈ int / float / length。ref_sorted 为被引用列升序取值；
    observed 为数据观测 (min, max)，用于 serial 等无声明值域的列。
    """
    kind = col.dist_kind
    if kind == "categorical":
        return ("categorical", list(col.distribution["values"]))
    if col.type == "bool":
        return ("categorical", [True, False])
    if col.type == "float":
        return ("float", float(col.min), float(col.max))
    if col.type == "string":
        return ("length", col.min_len, col.max_len)
    # int 列
    if kind in (None, "uniform", "zipf"):
        if col.min is not None:
            return ("int", col.min, col.max)
        return ("int", observed[0], observed[1])
    if kind == "serial":
        return ("int", observed[0], observed[1])
    if kind == "offset":
        base = table.column(col.distribution["column"])
        return ("int", base.min + col.distribution["min"],
                base.max + col.distribution["max"])
    if kind in ("ref_uniform", "ref_zipf"):
        return ("int", ref_sorted[0], ref_sorted[-1])
    raise ValueError(f"未知分布 {kind}")


def make_bucket_spec(col, table, schema, ref_sorted=None, observed=None):
    domain = column_domain(col, table, schema, ref_sorted, observed)
    if domain[0] == "categorical":
        values = domain[1]
        keys = [categorical_key(v) for v in values]
        return BucketSpec("categorical", keys, values=values)
    kind, lo, hi = domain
    if kind == "float":
        k = col.hist_bins
        boundaries = float_boundaries(lo, hi, k)
        keys = range_keys(boundaries, hi, float_fmt=True)
    elif kind == "length":
        k = min(col.hist_bins, hi - lo + 1)
        boundaries = int_boundaries(lo, hi, k)
        keys = range_keys(boundaries, hi, prefix="L")
    else:
        k = min(col.hist_bins, hi - lo + 1)
        boundaries = int_boundaries(lo, hi, k)
        keys = range_keys(boundaries, hi)
    return BucketSpec(kind, keys, boundaries=boundaries)


def _zipf_weights(n, exponent):
    return [(i + 1) ** (-exponent) for i in range(n)]


def int_pmf(col, table, schema):
    """int 列取值的概率质量函数，返回 (lo, probs)，probs[i] 对应 lo+i。"""
    kind = col.dist_kind
    if kind in (None, "uniform"):
        n = col.max - col.min + 1
        return col.min, [1.0 / n] * n
    if kind == "zipf":
        n = col.max - col.min + 1
        weights = _zipf_weights(n, col.distribution["exponent"])
        total = sum(weights)
        return col.min, [w / total for w in weights]
    if kind == "categorical":
        values = col.distribution["values"]
        weights = col.distribution["weights"]
        total = float(sum(weights))
        lo, hi = min(values), max(values)
        probs = [0.0] * (hi - lo + 1)
        for v, w in zip(values, weights):
            probs[v - lo] += w / total
        return lo, probs
    if kind == "offset":
        base = table.column(col.distribution["column"])
        base_lo, base_probs = int_pmf(base, table, schema)
        omin, omax = col.distribution["min"], col.distribution["max"]
        span = omax - omin + 1
        out = [0.0] * (len(base_probs) + span - 1)
        share = 1.0 / span
        for i, p in enumerate(base_probs):
            if p:
                for j in range(span):
                    out[i + j] += p * share
        return base_lo + omin, out
    raise ValueError(f"分布 {kind} 无解析 PMF")


def expected_ratios(col, table, schema, spec, ref_sorted=None):
    """判桶列各桶的期望占比（已乘 1 - null_rate）。"""
    kind = col.dist_kind
    non_null = 1.0 - col.null_rate
    if kind == "categorical":
        weights = col.distribution["weights"]
        total = float(sum(weights))
        ratios = [non_null * w / total for w in weights]
        # 动态追加的声明外桶期望为 0
        ratios.extend([0.0] * (spec.k - len(ratios)))
        return ratios
    if col.type == "float":
        return [non_null / spec.k] * spec.k
    if col.type == "string":
        # 长度均匀：每桶占比 = 桶内长度个数 / 长度总数
        lo, hi = col.min_len, col.max_len
        n = hi - lo + 1
        b = spec.boundaries
        return [non_null * (b[i + 1] - b[i]) / n for i in range(spec.k)]
    # int 列
    if kind in (None, "uniform"):
        lo, hi = col.min, col.max
        n = hi - lo + 1
        b = spec.boundaries
        return [non_null * (b[i + 1] - b[i]) / n for i in range(spec.k)]
    if kind == "zipf":
        n = col.max - col.min + 1
        weights = _zipf_weights(n, col.distribution["exponent"])
        total = sum(weights)
        prefix = [0.0]
        for w in weights:
            prefix.append(prefix[-1] + w)
        b = spec.boundaries
        out = []
        for i in range(spec.k):
            a = max(b[i] - col.min, 0)
            c = min(b[i + 1] - col.min, n)
            out.append(non_null * (prefix[c] - prefix[a]) / total)
        return out
    if kind == "offset":
        lo, probs = int_pmf(col, table, schema)
        prefix = [0.0]
        for p in probs:
            prefix.append(prefix[-1] + p)
        b = spec.boundaries
        out = []
        for i in range(spec.k):
            a = max(b[i] - lo, 0)
            c = min(b[i + 1] - lo, len(probs))
            out.append(non_null * (prefix[c] - prefix[a]))
        return out
    if kind == "ref_uniform":
        n = len(ref_sorted)
        b = spec.boundaries
        out = []
        for i in range(spec.k):
            cnt = (bisect.bisect_left(ref_sorted, b[i + 1])
                   - bisect.bisect_left(ref_sorted, b[i]))
            out.append(non_null * cnt / n)
        return out
    if kind == "ref_zipf":
        n = len(ref_sorted)
        exponent = col.distribution["exponent"]
        weights = _zipf_weights(n, exponent)
        total = sum(weights)
        prefix = [0.0]
        for w in weights:
            prefix.append(prefix[-1] + w)
        b = spec.boundaries
        out = []
        for i in range(spec.k):
            a = bisect.bisect_left(ref_sorted, b[i])
            c = bisect.bisect_left(ref_sorted, b[i + 1])
            out.append(non_null * (prefix[c] - prefix[a]) / total)
        return out
    raise ValueError(f"分布 {kind} 不判桶")


def is_bucket_judged(col, table, schema):
    """该列是否产生 bucket 检查（与 samples/expected 口径一致）。"""
    kind = col.dist_kind
    if kind is None or kind == "serial":
        return False
    if kind == "categorical":
        return True
    if col.type != "int":
        return False
    if kind in ("uniform", "zipf", "offset"):
        return True
    if kind in ("ref_uniform", "ref_zipf"):
        fk = schema.fk_for_ref_column(table, col.name)
        return fk is not None and fk.ref_table != table.name
    return False
