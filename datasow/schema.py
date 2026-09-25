"""schema JSON 的加载与结构校验。

任何非法输入都抛 SchemaError，由 CLI 映射为退出码 2。
"""
from __future__ import annotations

import json

TYPES = ("int", "float", "bool", "string")
DIST_KINDS = ("serial", "uniform", "categorical", "zipf", "offset",
              "ref_uniform", "ref_zipf")
CHECK_OPS = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">=", "ne": "!="}
DEFAULT_CHARSET = "abcdefghijklmnopqrstuvwxyz"
DEFAULT_HIST_BINS = 10
DEFAULT_TOLERANCE = 0.02


class SchemaError(Exception):
    pass


class Column:
    __slots__ = ("name", "type", "min", "max", "precision", "min_len",
                 "max_len", "charset", "not_null", "null_rate", "unique",
                 "distribution", "hist_bins", "tolerance")

    def __init__(self, name, type, **kw):
        self.name = name
        self.type = type
        self.min = kw.get("min")
        self.max = kw.get("max")
        self.precision = kw.get("precision", 3)
        self.min_len = kw.get("min_len")
        self.max_len = kw.get("max_len")
        self.charset = kw.get("charset", DEFAULT_CHARSET)
        self.not_null = bool(kw.get("not_null", False))
        self.null_rate = float(kw.get("null_rate", 0.0))
        self.unique = bool(kw.get("unique", False))
        self.distribution = kw.get("distribution")
        self.hist_bins = int(kw.get("hist_bins", DEFAULT_HIST_BINS))
        self.tolerance = float(kw.get("tolerance", DEFAULT_TOLERANCE))

    @property
    def dist_kind(self):
        return self.distribution["kind"] if self.distribution else None


class Check:
    __slots__ = ("kind", "left", "right")

    def __init__(self, kind, left, right):
        self.kind = kind
        self.left = left
        self.right = right

    @property
    def op(self):
        return CHECK_OPS[self.kind]


class ForeignKey:
    __slots__ = ("columns", "ref_table", "ref_columns", "null_ok")

    def __init__(self, columns, ref_table, ref_columns, null_ok):
        self.columns = columns
        self.ref_table = ref_table
        self.ref_columns = ref_columns
        self.null_ok = null_ok


class Table:
    __slots__ = ("name", "rows", "columns", "unique_keys", "checks",
                 "foreign_keys", "col_map")

    def __init__(self, name, rows, columns, unique_keys, checks, foreign_keys):
        self.name = name
        self.rows = rows
        self.columns = columns
        self.unique_keys = unique_keys
        self.checks = checks
        self.foreign_keys = foreign_keys
        self.col_map = {c.name: c for c in columns}

    def column(self, name):
        return self.col_map[name]


class Schema:
    __slots__ = ("version", "seed", "tables", "table_map")

    def __init__(self, version, seed, tables):
        self.version = version
        self.seed = seed
        self.tables = tables
        self.table_map = {t.name: t for t in tables}

    def table(self, name):
        return self.table_map[name]

    def fk_for_ref_column(self, table, col_name):
        """返回以 col_name 为唯一来源列的外键，没有则 None。"""
        for fk in table.foreign_keys:
            if fk.columns == [col_name]:
                return fk
        return None


def _err(msg):
    raise SchemaError(msg)


def _require(cond, msg):
    if not cond:
        _err(msg)


def _load_column(table_name, raw):
    _require(isinstance(raw, dict), f"表 {table_name}: 列定义必须是对象")
    name = raw.get("name")
    _require(isinstance(name, str) and name, f"表 {table_name}: 列缺少 name")
    ctype = raw.get("type")
    _require(ctype in TYPES, f"列 {name}: 非法 type {ctype!r}")
    col = Column(name, ctype, **{k: v for k, v in raw.items()
                                 if k not in ("name", "type")})
    _require(isinstance(col.precision, int) and 0 <= col.precision <= 10,
             f"列 {name}: precision 非法")
    _require(0.0 <= col.null_rate <= 1.0, f"列 {name}: null_rate 越界")
    if col.not_null:
        _require(col.null_rate == 0.0,
                 f"列 {name}: not_null 为 true 时 null_rate 必须为 0")
    if col.unique:
        _require(col.null_rate == 0.0,
                 f"列 {name}: unique 列不许为空")
    _require(col.hist_bins >= 1, f"列 {name}: hist_bins 必须 >= 1")
    _require(col.tolerance > 0, f"列 {name}: tolerance 必须为正")

    if ctype == "int":
        if col.dist_kind in (None, "uniform", "zipf"):
            _require(isinstance(col.min, int) and isinstance(col.max, int)
                     and not isinstance(col.min, bool)
                     and not isinstance(col.max, bool),
                     f"列 {name}: int 列需要整数 min/max")
            _require(col.min <= col.max, f"列 {name}: min > max")
    elif ctype == "float":
        _require(isinstance(col.min, (int, float))
                 and isinstance(col.max, (int, float))
                 and not isinstance(col.min, bool)
                 and not isinstance(col.max, bool),
                 f"列 {name}: float 列需要数值 min/max")
        _require(col.min < col.max, f"列 {name}: min 必须小于 max")
    elif ctype == "string":
        if col.dist_kind != "categorical":
            _require(isinstance(col.min_len, int)
                     and isinstance(col.max_len, int),
                     f"列 {name}: string 列需要 min_len/max_len")
            _require(0 <= col.min_len <= col.max_len,
                     f"列 {name}: min_len/max_len 非法")
            _require(isinstance(col.charset, str) and len(col.charset) > 0,
                     f"列 {name}: charset 不能为空")

    dist = col.distribution
    if dist is not None:
        _require(isinstance(dist, dict), f"列 {name}: distribution 必须是对象")
        kind = dist.get("kind")
        _require(kind in DIST_KINDS, f"列 {name}: 未知分布 {kind!r}")
        if kind == "serial":
            _require(ctype == "int", f"列 {name}: serial 仅支持 int")
            start = dist.get("start", 0)
            step = dist.get("step", 1)
            _require(isinstance(start, int) and isinstance(step, int)
                     and step != 0, f"列 {name}: serial start/step 非法")
        elif kind == "categorical":
            values = dist.get("values")
            weights = dist.get("weights")
            _require(isinstance(values, list) and values,
                     f"列 {name}: categorical 需要 values")
            _require(isinstance(weights, list)
                     and len(weights) == len(values),
                     f"列 {name}: weights 与 values 等长")
            _require(all(isinstance(w, (int, float)) and w >= 0
                         and not isinstance(w, bool) for w in weights),
                     f"列 {name}: weights 必须为非负数")
            _require(sum(weights) > 0, f"列 {name}: weights 全为 0")
            for v in values:
                _check_value_type(name, ctype, v)
        elif kind == "zipf":
            _require(ctype == "int", f"列 {name}: zipf 仅支持 int")
            _require(isinstance(dist.get("exponent"), (int, float))
                     and dist["exponent"] > 0,
                     f"列 {name}: zipf exponent 必须为正")
        elif kind == "offset":
            _require(ctype == "int", f"列 {name}: offset 仅支持 int")
            _require(isinstance(dist.get("column"), str),
                     f"列 {name}: offset 需要 column")
            _require(isinstance(dist.get("min"), int)
                     and isinstance(dist.get("max"), int)
                     and dist["min"] <= dist["max"],
                     f"列 {name}: offset min/max 非法")
        elif kind in ("ref_uniform", "ref_zipf"):
            _require(ctype == "int", f"列 {name}: {kind} 仅支持 int")
            if kind == "ref_zipf":
                _require(isinstance(dist.get("exponent"), (int, float))
                         and dist["exponent"] > 0,
                         f"列 {name}: ref_zipf exponent 必须为正")
    return col


def _check_value_type(name, ctype, value):
    ok = ((ctype == "int" and isinstance(value, int)
           and not isinstance(value, bool))
          or (ctype == "float" and isinstance(value, (int, float))
              and not isinstance(value, bool))
          or (ctype == "bool" and isinstance(value, bool))
          or (ctype == "string" and isinstance(value, str)))
    _require(ok, f"列 {name}: categorical 取值 {value!r} 与类型 {ctype} 不符")


def _load_table(raw):
    _require(isinstance(raw, dict), "表定义必须是对象")
    name = raw.get("name")
    _require(isinstance(name, str) and name, "表缺少 name")
    rows = raw.get("rows")
    _require(isinstance(rows, int) and not isinstance(rows, bool) and rows > 0,
             f"表 {name}: rows 必须为正整数")
    raw_cols = raw.get("columns")
    _require(isinstance(raw_cols, list) and raw_cols,
             f"表 {name}: columns 不能为空")
    columns = [_load_column(name, c) for c in raw_cols]
    seen = set()
    for c in columns:
        _require(c.name not in seen, f"表 {name}: 列名重复 {c.name}")
        seen.add(c.name)
    col_map = {c.name: c for c in columns}

    unique_keys = raw.get("unique_keys", [])
    _require(isinstance(unique_keys, list), f"表 {name}: unique_keys 必须是数组")
    for uk in unique_keys:
        _require(isinstance(uk, list) and len(uk) >= 2,
                 f"表 {name}: 复合唯一键至少两列")
        for cn in uk:
            _require(cn in col_map, f"表 {name}: 唯一键列不存在 {cn}")
            _require(col_map[cn].null_rate == 0.0,
                     f"表 {name}: 唯一键列 {cn} 不许为空")

    checks = []
    for raw_ck in raw.get("checks", []):
        _require(isinstance(raw_ck, dict), f"表 {name}: check 必须是对象")
        kind = raw_ck.get("kind")
        _require(kind in CHECK_OPS, f"表 {name}: 未知 check kind {kind!r}")
        left, right = raw_ck.get("left"), raw_ck.get("right")
        _require(left in col_map and right in col_map,
                 f"表 {name}: check 引用了不存在的列")
        checks.append(Check(kind, left, right))

    foreign_keys = []
    for raw_fk in raw.get("foreign_keys", []):
        _require(isinstance(raw_fk, dict), f"表 {name}: foreign_key 必须是对象")
        fcols = raw_fk.get("columns")
        rcols = raw_fk.get("ref_columns")
        rtable = raw_fk.get("ref_table")
        _require(isinstance(fcols, list) and fcols
                 and isinstance(rcols, list) and len(fcols) == len(rcols),
                 f"表 {name}: 外键 columns/ref_columns 非法")
        _require(isinstance(rtable, str) and rtable,
                 f"表 {name}: 外键缺少 ref_table")
        for cn in fcols:
            _require(cn in col_map, f"表 {name}: 外键列不存在 {cn}")
        foreign_keys.append(ForeignKey(fcols, rtable, rcols,
                                       bool(raw_fk.get("null_ok", False))))
    return Table(name, rows, columns, unique_keys, checks, foreign_keys)


def _cross_validate(schema):
    positions = {t.name: i for i, t in enumerate(schema.tables)}
    for table in schema.tables:
        col_map = table.col_map
        for fk in table.foreign_keys:
            _require(fk.ref_table in schema.table_map,
                     f"表 {table.name}: 被引用表不存在 {fk.ref_table}")
            _require(positions[fk.ref_table] <= positions[table.name],
                     f"表 {table.name}: 被引用表 {fk.ref_table} 必须在前或为自身")
            ref = schema.table(fk.ref_table)
            for cn in fk.ref_columns:
                _require(cn in ref.col_map,
                         f"表 {table.name}: 被引用列不存在 {fk.ref_table}.{cn}")
            target_ok = False
            if len(fk.ref_columns) == 1:
                target_ok = ref.col_map[fk.ref_columns[0]].unique
            if not target_ok:
                target_ok = any(sorted(uk) == sorted(fk.ref_columns)
                                for uk in ref.unique_keys)
            _require(target_ok,
                     f"表 {table.name}: 外键目标 {fk.ref_table}.{fk.ref_columns} "
                     "必须是 unique 列或 unique_keys")
        for col in table.columns:
            kind = col.dist_kind
            if kind == "offset":
                base_name = col.distribution["column"]
                _require(base_name in col_map,
                         f"列 {table.name}.{col.name}: offset 基准列不存在")
                base = col_map[base_name]
                _require(base.type == "int",
                         f"列 {table.name}.{col.name}: offset 基准列必须是 int")
                _require(base.min is not None and base.max is not None,
                         f"列 {table.name}.{col.name}: offset 基准列需要 min/max")
                _require(table.columns.index(base) < table.columns.index(col),
                         f"列 {table.name}.{col.name}: offset 基准列必须在前")
            elif kind in ("ref_uniform", "ref_zipf"):
                fk = schema.fk_for_ref_column(table, col.name)
                _require(fk is not None,
                         f"列 {table.name}.{col.name}: {kind} 需要对应外键声明")
            if col.unique and kind not in (None, "serial", "uniform"):
                _err(f"列 {table.name}.{col.name}: unique 仅支持 serial/uniform")
            if col.unique and kind != "serial":
                _check_unique_space(table, col, table.rows)


def _check_unique_space(table, col, rows):
    if col.type == "int":
        space = col.max - col.min + 1
    elif col.type == "string":
        space = sum(len(col.charset) ** n
                    for n in range(col.min_len, col.max_len + 1))
    else:
        _err(f"列 {table.name}.{col.name}: 该类型不支持非 serial 唯一取值")
    _require(space >= rows,
             f"列 {table.name}.{col.name}: 取值空间 {space} 小于行数 {rows}")


def check_unique_space(schema, table, col, rows):
    """生成时按实际行数复查唯一列取值空间。"""
    _check_unique_space(table, col, rows)


def load_schema(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise SchemaError(f"无法读取 schema: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(f"schema 不是合法 JSON: {exc}") from exc
    _require(isinstance(raw, dict), "schema 顶层必须是对象")
    _require(isinstance(raw.get("version"), int), "schema 缺少 version")
    seed = raw.get("seed")
    if seed is not None:
        _require(isinstance(seed, int) and not isinstance(seed, bool),
                 "schema seed 必须是整数")
    raw_tables = raw.get("tables")
    _require(isinstance(raw_tables, list) and raw_tables, "schema 缺少 tables")
    tables = [_load_table(t) for t in raw_tables]
    names = [t.name for t in tables]
    _require(len(set(names)) == len(names), "表名重复")
    schema = Schema(raw["version"], seed, tables)
    _cross_validate(schema)
    return schema
