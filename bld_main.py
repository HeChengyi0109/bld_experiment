# bld_main.py
from bld_solver import solve_benders_model
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
        "--vehicle-time-limit",
        type=float,
        default=None,
        help=(
            "预留接口：单车子问题的附加 SCIP 时间上限，单位秒。"
            "默认 None 表示只使用动态分配策略：第 k 辆车预算 = (Params!B12 - 已用时间) / 剩余车辆数。"
            "若传入正数，则实际单车时限取 min(动态预算, 该附加上限)。"
        ),
    )

    args = parser.parse_args()
    input_file = args.input
    output_file = args.output

    # 1) 输入文件存在性检查
    if not os.path.exists(input_file):
        print(f"[error]输入文件不存在：{input_file}")
        sys.exit(1)

    # ==========================
    # 2) precheck 模式：只读 Params，不读 inv_SKUs/Prices
    # ==========================
    if args.mode == "precheck":
        try:
            I, V = read_params_only(input_file)  # 你已在 bld_data.py 中新增
        except Exception as e:
            print(f"[error]读取 Params 失败：{e}")
            sys.exit(1)

        check = precheck_counts(int(I), int(V))
        print(check.message)
        if not check.ok:
            sys.exit(1)

        # 只做预检查，到此结束
        return

    # ==========================
    # 3) solve 模式：读取全量数据 + 校验 + 预检查 + 求解
    # ==========================
    print("正在读取数据(Excel).")
    data = read_excel_data(input_file)
    if data is None:
        print("[error]数据读取失败，程序退出。")
        sys.exit(1)

    # 原有数据校验（仅 solve 时做）
    try:
        validate_data(data.__dict__)
    except Exception as e:
        print(f"[error]数据校验失败：{e}")
        sys.exit(1)

    # 订单参数预检查（仅 I/V，不依赖 SKU 属性；solve 时也先拦一遍）
    check = precheck_counts(int(data.num_of_SKUs), int(data.num_of_Vehicles))
    print(check.message)
    if not check.ok:
        sys.exit(1)

    print("进入优化模型求解.")

    # 只有真的要 solve 时，才延迟导入 solver（避免在 precheck 触发 pyscipopt 相关问题）

    # 求解
    solution, saved_file, master, subprob = solve_benders_model(
        data,
        input_file=input_file,
        output_file=output_file,
        vehicle_time_limit=args.vehicle_time_limit,
    )

    # 结果判定（保持你原来的逻辑）
    if saved_file is not None and solution.status in ["optimal", "timelimit", "gaplimit"]:
        print(f"[success]求解完成! 结果已保存到 {saved_file}")
    elif solution.status in ["infeasible", "inforunbd", "unbounded"] or saved_file is None:
        print(f"[error]当前SKU输入无法达到配矿需求，无可行解，请重新输入SKU（更换筛选方式或数量），状态: {solution.status}")
        sys.exit(1)
    else:
        print(f"[error]求解失败，状态: {solution.status}")
        sys.exit(1)


if __name__ == "__main__":
    main()
