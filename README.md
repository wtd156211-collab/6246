# wt-038 datasow 约束驱动的测试数据生成（从 0 实现）

起始环境只有说明与素材，`web/` 空着，引擎、校验器与页面从零写。Python 3.13、标准库；页面是原生 HTML + Canvas，无构建、无
依赖。入口固定 `python -m datasow <子命令>`。

## 1. 范围

做的：读 `samples/schema/*.json` 的表定义，按声明的类型、范围、唯一性、非空、字段间不等式与外键生成数据；分布按声明抽样、
按种子可复现、按容差可校验；边生成边落盘；写 `out/<表>.jsonl` 与校验报告；页面画出每字段直方图、唯一值个数与校验结果。

不做：`samples/` 之外的输入、数据库、增量续跑、多进程、相关性等业务语义、通用约束求解、额外统计量、图表库与前端框架、构建
步骤与第三方依赖。

## 2. 口径与公式

### 2.1 桶与期望占比

- 分桶：分类列每值一桶；`int`/`float` 按值域分 `hist_bins`（默认 10）个等宽桶，左闭右开、末桶右闭；字符串列按**长度**分桶；空值
  只算 `null_count`/`null_ratio`。桶键 `=值`、`[a,b)`、`L[a,b)`；`K = min(hist_bins, 可取个数)`（`float` 取 `hist_bins`），整数
  域用 `p_k = lo + floor(k×N/K)`、`N = hi−lo+1`，`float` 用 `lo + (hi−lo)×k/K`。值域取列的 `min`/`max`，`offset` 列取基准列
  值域加增量范围，外键列取被引用列的取值集合。
- 期望占比（分母 = 总行数，含空值）：非空各桶 `(1 − null_rate) × 桶内权重和 / 总权重`，空值 = `null_rate`。权重：`uniform`
  每值等权（`float` 每桶 `1/K`）、`categorical` 用 `weights`、`zipf` 按 `k^−exponent`、`offset` 是基准分布与均匀增量的卷积；
  `serial` 与自引用外键列不判桶。

### 2.2 容差与判定

- 判定 `|实测占比 − 期望占比| ≤ 容差`，容差是绝对差、默认 `0.02`，列上 `tolerance` 覆盖，期望为 0 的桶必须实测为 0。
- 唯一性/外键靠重抽或结构化取值满足，扰动由容差吸收；单行重抽上限 1000 次，超限致命退出；`unique` 列与复合唯一键的列不许
  为空、取值空间 ≥ 行数。

### 2.3 种子与复现

- 同 schema + 同 `--seed` + 同参数，两次运行**逐字节相同**（`sha256` 一致）；`--seed` 覆盖 schema 里的 `seed`，都没有则报错
  退出。不许依赖系统时间、进程号、`os.urandom`、集合迭代顺序；换 seed 数据要变、判定仍全过。

## 3. 数据结构（schema JSON）

严格 JSON（UTF-8、无注释、无尾逗号），完整示例见 `samples/schema/*.json`；顶层是 `{"version", "seed", "tables": [...]}`，
表是 `{"name", "rows", "columns": [...], "unique_keys", "checks", "foreign_keys"}`，列是 `{"name", "type", ...}`。

- 列：`type` 取 `int`（`min`/`max` 闭区间）、`float`（另有 `precision`，默认 3）、`bool`、`string`（`min_len`/`max_len`/
  `charset`）；`not_null` 默认 false、`null_rate` 默认 0（`not_null` 为 true 时必须为 0）、`unique` 默认 false；未声明
  `distribution` 的列不判桶、按类型默认均匀取值。
- 分布：`serial`（`start`/`step`）、`uniform`、`categorical`（`values` 加等长 `weights`）、`zipf`（`int`、`exponent` > 0）、
  `offset`（`int`，`column` 指向同表列，值为基准值加 `[min,max]` 均匀整数）、`ref_uniform`/`ref_zipf`（外键列，在被引用列已
  落盘的取值里抽样）。
- 约束：`unique_keys` 每项一组列、组合全表唯一；`checks`（字段间大小关系）取 `lt`/`lte`/`gt`/`gte`/`ne`、逐行判定、空值行跳过；外键
  `ref_columns` 必须是被引用表的 `unique` 列或 `unique_keys`，`null_ok` 默认 false；表按 `tables` 顺序生成，被引用表在前或
  为自身，自引用只引用已落盘的行。

## 4. 输入输出与文件格式

### 4.1 命令行与产物

```
python -m datasow gen --schema <schema.json> --out <目录> [--seed N] [--rows-scale F] [--batch-size N]
python -m datasow validate --schema <schema.json> --data <目录> --report <report.json>
```

仓库根目录执行。`gen` 写 `<目录>/<表名>.jsonl`（目录不存在就建），行数 = `ceil(rows × rows-scale)`（默认 1.0）；每
累计 `--batch-size`（默认 2048）行 flush 一次，中断后已落盘的每一行都是完整 JSON；退出码 0 成功、2 参数或 schema 非法、1
运行中失败。`validate` 只读数据目录做统计与判定，退出码 0 全过、1 有 fail（仍写报告）、2 参数或数据不可读。

数据文件是 JSONL：UTF-8、单 `\n`、末尾换行，每行一个对象，键顺序 = 列顺序，`int` 不带小数点、`float` 按 `precision` 写十进制、
`bool` 写 `true`/`false`、空值写 `null`。

### 4.2 校验报告 `report.json`

UTF-8、缩进 2、末尾换行。必需字段：`schema`、`seed`、`rows_scale`；`tables[]` 的 `name`、`rows`、`columns[]`（`name`、`type`、
`null_count`、`unique_count`、`buckets[]` 的 `key`/`count`/`ratio`）、`checks[]` 的 7 个字段（`id`、`kind`、`status`、`expected`、
`measured`、`tolerance`、`detail`）；`summary` 的 `total`/`pass`/`fail`。`status` 只有 `pass`/`fail`，`ratio` 6 位小数，`id`
与 `samples/expected/<样例>.txt` 逐行对应，判定口径见 `samples/expected/checks.md`。

### 4.3 展示这一侧

- 打开：仓库根目录 `python -m http.server 8000`，开
  `http://127.0.0.1:8000/web/index.html?report=out/01/report.json`（`report` 相对仓库根，缺省 `out/report.json`）；`file://`
  打不开，页面要 fetch，取不到就提示，并给出加载中/失败状态。
- 页面不算统计、不读 `samples/`，只按报告画图。必须体现：① 每表每字段一张直方图（Canvas 柱状，x 轴桶键、y 轴占比，标 `count`
  与 `ratio`）；② 每字段唯一值个数与空值数；③ 全部检查逐条列出并标 `status`，fail 显著标出；④ 顶部汇总与来源路径。

## 5. 性能与验收口径

### 5.1 规模与预算

三个样例合计 25.5 万行（最大单表 6 万行），`--rows-scale` 可放大。测试机（Windows + Python 3.13、单进程单线程）：单样例
`gen` ≤ 60 秒、峰值工作集 ≤ 300 MiB；`--rows-scale 4` 相对 1.0 的峰值增量 ≤ 48 MiB；`validate` ≤ 90 秒、≤ 400 MiB。

### 5.2 验收（逐条）

1. Python 3.13、标准库、无构建；两条子命令与页面都能跑起来。
2. `out/*.jsonl` 每行合法 JSON、键集合与顺序等于列、类型一致、行数 = `ceil(rows × scale)`。
3. `rows`、`columns`、`nullratio`、`unique`、`uk`、`fk`、`ineq` 全 `pass`。
4. `report.json` 的 `bucket` 检查与 `samples/expected/<样例>.txt` 逐行一致。
5. 同一 seed 跑两遍 `sha256` 一致；5.1 的数字达标；中断后已落盘的每一行都是完整 JSON。
6. 页面四项信息齐全、数字与 `report.json` 一致；`python -m unittest discover` 跑通，测试只读 `samples/`。

## 6. 样例说明

每份样例一对文件（`samples/schema/<样例>.json` + `samples/expected/<样例>.txt`），口径与 id 语法见
`samples/expected/checks.md`；`samples/notes.md` 是现场记录。

- `01_orders`（3 万 + 6 万）：`categorical`（5 值字符串、`bool`）；`uniform` 整数与浮点；`ref_zipf` 热点客户；`offset` 加
  `lt`；`null_rate` 0.05 的可空字符串；一列 `tolerance` 0.015。
- `02_catalog`（2.5 + 2.5 + 3 万）：自引用外键 `parent_id → category_id`（8% 根节点、`parent_id < category_id`）；复合唯一键
  `(tenant_id, code)`；字符串按长度分桶。
- `03_sensor`（2.5 万 + 6 万）：最大规模；非外键 `zipf`（`battery_pct` 偏斜）；`offset` 加 `lte`；定长字符串一列
  `tolerance` 0.001；复合唯一键 `(device_id, trace_id)`。

## 7. 待补的文档

相关性/时间自相关怎么声明；schema 版本演进与兼容；`report.json` 之外的报表与页面导出；分片生成与合并；错误码与文案；页面
配色与分页。
