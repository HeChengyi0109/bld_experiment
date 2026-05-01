# bld_main.py
import argparse
import os
import sys

from config import INPUT_FILE, OUTPUT_FILE
from bld_data import read_excel_data, read_params_only
from bld_utils import validate_data, precheck_counts
from bld_solver import solve_benders_model


def main():
    parser = argparse.ArgumentParser(description="BLD 配矿模型求解器（顺序解热启动 + 整体 MIP）")
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
        "--disable-warmstart",
        action="store_true",
        help="禁用顺序配矿热启动，直接冷启动求解整体 MIP"
    )
    parser.add_argument(
        "--main-time-limit",
        type=float,
        default=None,
        help=(
            "逐车 warmstart 初始解注入 SCIP 后，整体 MIP 主求解阶段的时间上限（秒）。"
            "不传时使用 bld_solver.py 中的 FORCE_MAIN_MIP_TIME_LIMIT_SEC；"
            "若该常量为 None，则回退使用 Excel Params!B12。"
        ),
    )
    parser.add_argument(
        "--vehicle-time-limit",
        type=float,
        default=None,
        help=(
            "预留接口：warmstart 逐车阶段每个单车 MIP 的附加 SCIP 时间上限，单位秒。"
            "默认 None 表示只使用动态分配策略：第 k 辆车预算 = (Params!B12 - 已用时间) / 剩余车辆数。"
            "若传入正数，则实际单车时限取 min(动态预算, 该附加上限)。"
        ),
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

    solution, saved_file, master, subprob = solve_benders_model(
        data,
        input_file=input_file,
        output_file=output_file,
        use_warmstart=not args.disable_warmstart,
        main_time_limit_sec=args.main_time_limit,
        vehicle_time_limit=args.vehicle_time_limit,
    )

    has_solution = getattr(solution, "has_solution", False)

    if has_solution and saved_file is not None and solution.status in ["optimal", "timelimit", "gaplimit"]:
        print(f"[success]求解完成! 结果已保存到 {saved_file}")
    elif solution.status in ["infeasible", "inforunbd", "unbounded"]:
        print(f"[error]当前SKU输入无法达到配矿需求，无可行解，请重新输入SKU（更换筛选方式或数量），状态: {solution.status}")
        sys.exit(1)
    elif solution.status in ["timelimit", "gaplimit"] and not has_solution:
        print(f"[error]求解在时限/Gap限制内结束，但没有找到任何可行解，状态: {solution.status}")
        sys.exit(1)
    else:
        print(f"[error]求解失败，状态: {solution.status}")
        sys.exit(1)


if __name__ == "__main__":
    main()
