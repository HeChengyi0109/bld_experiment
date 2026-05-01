# bld_main.py
from bld_solver import solve_benders_model, solve_greedy_heuristic
import argparse
import os
import sys

from config import INPUT_FILE, OUTPUT_FILE
from bld_data import read_excel_data, read_params_only
from bld_utils import validate_data, precheck_counts


def main():
    parser = argparse.ArgumentParser(description="BLD 配矿模型求解器")
    parser.add_argument(
        "--input",
        type=str,
        default=INPUT_FILE,
        help="输入 Excel 文件路径"
    )
    parser.add_argument(
        "--output",
        type=str,
        default=OUTPUT_FILE,
        help="输出 Excel 文件路径"
    )
    parser.add_argument(
        "--mode",
        choices=["precheck", "solve"],
        default="solve",
        help="运行模式：precheck=只做订单参数预检查；solve=预检查+求解"
    )
    parser.add_argument(
        "--method",
        choices=["scip", "greedy"],
        default="greedy",
        help="求解方法：scip=原单体 MIP；greedy=硬约束版 greedy"
    )

    args = parser.parse_args()
    input_file = args.input
    output_file = args.output

    if not os.path.exists(input_file):
        print(f"[error]输入文件不存在：{input_file}")
        sys.exit(1)

    if args.mode == "precheck":
        try:
            I, V = read_params_only(input_file)
        except Exception as e:
            print(f"[error]读取 Params 失败：{e}")
            sys.exit(1)

        check = precheck_counts(int(I), int(V))
        print(check.message)
        if not check.ok:
            sys.exit(1)
        return

    print("正在读取数据(Excel).")
    data = read_excel_data(input_file)
    if data is None:
        print("[error]数据读取失败，程序退出。")
        sys.exit(1)

    try:
        validate_data(data.__dict__)
    except Exception as e:
        print(f"[error]数据校验失败：{e}")
        sys.exit(1)

    check = precheck_counts(int(data.num_of_SKUs), int(data.num_of_Vehicles))
    print(check.message)
    if not check.ok:
        sys.exit(1)

    print("进入优化模型求解.")

    if args.method == "scip":
        solution, saved_file, master, subprob = solve_benders_model(
            data,
            input_file=input_file,
            output_file=output_file,
        )
        ok_status = ["optimal", "timelimit", "gaplimit"]
    else:
        solution, saved_file, master, subprob = solve_greedy_heuristic(
            data,
            input_file=input_file,
            output_file=output_file,
        )
        ok_status = ["heuristic"]

    if saved_file is not None and solution.status in ok_status:
        print(f"[success]求解完成! 结果已保存到 {saved_file}")
    elif solution.status in ["infeasible", "inforunbd", "unbounded"] or saved_file is None:
        print(f"[error]当前SKU输入无法达到配矿需求，无可行解，请重新输入SKU（更换筛选方式或数量），状态: {solution.status}")
        sys.exit(1)
    else:
        print(f"[error]求解失败，状态: {solution.status}")
        sys.exit(1)


if __name__ == "__main__":
    main()
