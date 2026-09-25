"""命令行入口：python -m datasow gen|validate。

退出码：0 成功；2 参数或 schema/数据非法；1 运行中失败。
"""
from __future__ import annotations

import argparse
import sys

from .engine import GenerationError, generate
from .schema import SchemaError, load_schema
from .validate import DataError, validate, write_report


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="datasow", description="约束驱动的测试数据生成器")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("gen", help="按 schema 生成数据")
    gen.add_argument("--schema", required=True, help="schema JSON 路径")
    gen.add_argument("--out", required=True, help="输出目录")
    gen.add_argument("--seed", type=int, default=None,
                     help="覆盖 schema 里的 seed")
    gen.add_argument("--rows-scale", type=float, default=1.0,
                     help="行数缩放因子（默认 1.0）")
    gen.add_argument("--batch-size", type=int, default=2048,
                     help="每累计多少行 flush 一次（默认 2048）")

    val = sub.add_parser("validate", help="校验数据目录并写报告")
    val.add_argument("--schema", required=True, help="schema JSON 路径")
    val.add_argument("--data", required=True, help="数据目录")
    val.add_argument("--report", required=True, help="报告 JSON 输出路径")
    return parser


def _cmd_gen(args):
    schema = load_schema(args.schema)
    seed = args.seed if args.seed is not None else schema.seed
    if seed is None:
        print("错误: 未提供种子（--seed 与 schema.seed 至少其一）",
              file=sys.stderr)
        return 2
    if args.rows_scale <= 0:
        print("错误: --rows-scale 必须为正", file=sys.stderr)
        return 2
    if args.batch_size <= 0:
        print("错误: --batch-size 必须为正", file=sys.stderr)
        return 2
    tables = generate(schema, args.out, seed,
                      rows_scale=args.rows_scale,
                      batch_size=args.batch_size)
    for name in tables:
        print(f"已生成 {args.out}/{name}.jsonl")
    return 0


def _cmd_validate(args):
    schema = load_schema(args.schema)
    report = validate(schema, args.data, args.schema)
    write_report(report, args.report)
    summary = report["summary"]
    print(f"检查 {summary['total']} 条: 通过 {summary['pass']}，"
          f"失败 {summary['fail']}；报告已写入 {args.report}")
    return 0 if summary["fail"] == 0 else 1


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "gen":
            return _cmd_gen(args)
        return _cmd_validate(args)
    except SchemaError as exc:
        print(f"schema 非法: {exc}", file=sys.stderr)
        return 2
    except DataError as exc:
        print(f"数据不可读: {exc}", file=sys.stderr)
        return 2
    except GenerationError as exc:
        print(f"生成失败: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"文件错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
