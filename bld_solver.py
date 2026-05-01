"""
bld_benders_solver.py
顺序配矿求解模块：按车辆逐车求解 + 结果写回 Excel
"""
from dataclasses import replace
import time
import pandas as pd
from pyscipopt import SCIP_PARAMSETTING

from bld_data import ModelData, Solution, preprocess_sku_data
from bld_model import build_mip_model
from bld_utils import write_df_to_sheet


def extract_solution(model, x, data: ModelData, best_sol) -> Solution:
    """
    从 best_sol 提取最终解（适用于 optimal / timelimit / gaplimit 等情况）
    """
    status = str(model.getStatus())

    V_IDs = [0] * data.num_of_SKUs
    B_IDs = [0] * data.num_of_SKUs

    for i in range(data.num_of_SKUs):
        assigned = False
        for v in range(data.num_of_Vehicles):
            for k in range(data.num_of_Batches):
                if model.getSolVal(best_sol, x[i, v, k]) > 0.5:
                    V_IDs[i] = v + 1
                    B_IDs[i] = k + 1
                    assigned = True
                    break
            if assigned:
                break

    try:
        objective_value = float(model.getSolObjVal(best_sol))
    except Exception:
        objective_value = float(model.getObjVal())

    gap = float(model.getGap()) if hasattr(model, "getGap") else 0.0

    return Solution(
        status=status,
        objective_value=objective_value,
        V_IDs=V_IDs,
        B_IDs=B_IDs,
        gap=gap,
    )


def save_solution(solution: Solution, data: ModelData, input_file: str, output_file: str) -> str:
    """
    保存求解结果到 output.xlsx
    """
    df = data.SKUs_Data.reset_index(drop=True)
    out_skus = pd.DataFrame({
        "No": df["SKU_ID"],
        "V_ID": solution.V_IDs,
        "B_ID": solution.B_IDs,
        "Weight": df["weight"],
        "销售量": df.get("sale_weight", 0),
        "Cu": df["Cu_grade"],
        "Mo": df["Mo_grade"],
        "Water": df["water_percentage"],
    })
    write_df_to_sheet(output_file, "SKUs", out_skus, start_row=1, start_col=1, clear_sheet=True)

    rows = []
    V = data.num_of_Vehicles
    K = data.num_of_Batches

    for v in range(1, V + 1):
        for k in range(1, K + 1):
            idx = [i for i in range(data.num_of_SKUs) if solution.V_IDs[i] == v and solution.B_IDs[i] == k]

            W = sum(data.w[i] for i in idx) if idx else 0.0
            Wdry = sum(data._w[i] for i in idx) if idx else 0.0
            mo_metal = 100 * sum(data.beta[i] * data._w[i] for i in idx) if idx else 0.0
            cu_metal = 100 * sum(data.alpha[i] * data._w[i] for i in idx) if idx else 0.0

            mo_grade = (mo_metal / Wdry) if Wdry > 0 else 0.0
            cu_grade = (cu_metal / Wdry) if Wdry > 0 else 0.0
            moisture = ((W - Wdry) / W) if W > 0 else 0.0

            unit_price = float(data.a[k - 1])
            total_price = unit_price * mo_metal

            if W <= 0:
                continue

            rows.append({
                "V_ID": v,
                "B_ID": k,
                "W": W,
                "Wdry": Wdry,
                "Mo grade": mo_grade,
                "Cu grade": cu_grade,
                "moisture": moisture,
                "unit_price": unit_price,
                "Mo metal": mo_metal,
                "total_price": total_price,
            })

    vb_df = pd.DataFrame(rows, columns=[
        "V_ID", "B_ID", "W", "Wdry", "Mo grade", "Cu grade", "moisture",
        "unit_price", "Mo metal", "total_price"
    ])
    write_df_to_sheet(output_file, "VBResult", vb_df, start_row=1, start_col=1, clear_sheet=True)

    return output_file


def print_solution_summary(
    solution: Solution,
    data: ModelData,
    runtime_sec: float | None = None,
    saved_file: str | None = None,
):
    """
    打印求解摘要
    """

    def _fmt(x: float) -> str:
        try:
            return f"{float(x):.6g}"
        except Exception:
            return str(x)

    print("\n=== 求解结果摘要 ===")
    runtime_str = "N/A" if runtime_sec is None else f"{runtime_sec:.3f} s"
    print(f"[result]运行时间：{runtime_str}")
    print(f"[result]求解状态：{solution.status}")

    obj = solution.objective_value
    obj_str = "N/A" if obj is None else f"{obj:.6f}"
    print(f"[result]目标值：{obj_str}")

    used_vehicles = {v for v in solution.V_IDs if v and v > 0}
    assigned_skus = sum(1 for v in solution.V_IDs if v and v > 0)
    used_batches = {b for b in solution.B_IDs if b and b > 0}

    print(f"[result]使用批次数量：{len(used_batches)}")

    mo_intervals = set()
    cu_intervals = set()
    for b in sorted(used_batches):
        idx = b - 1
        if 0 <= idx < data.num_of_Batches:
            mo_intervals.add((data.beta_min[idx], data.beta_max[idx]))
            cu_intervals.add((data.alpha_min[idx], data.alpha_max[idx]))

    mo_str = "; ".join(f"{_fmt(lo)}–{_fmt(hi)}" for lo, hi in sorted(mo_intervals)) if mo_intervals else "N/A"
    cu_str = "; ".join(f"{_fmt(lo)}–{_fmt(hi)}" for lo, hi in sorted(cu_intervals)) if cu_intervals else "N/A"
    print(f"[result]使用批次钼品位区间：{mo_str}")
    print(f"[result]使用批次铜品位区间：{cu_str}")
    print(f"[result]使用车次数量：{len(used_vehicles)}")
    print(f"[result]使用SKU数量：{assigned_skus}")


def _solve_single_vehicle_mip(data: ModelData):
    """
    对“单车数据”求解一次 MIP。
    这里假定 data.num_of_Vehicles == 1。
    """
    model = build_mip_model(data)
    x = model.data["x"]

    print("配置 SCIP 求解参数 ...")
    try:
        model.setPresolve(SCIP_PARAMSETTING.OFF)
        model.setPropagating(SCIP_PARAMSETTING.DEFAULT)
        model.setHeuristics(SCIP_PARAMSETTING.DEFAULT)
    except Exception:
        pass

    if getattr(data, "run_epgap", 0) and float(data.run_epgap) > 0:
        model.setParam("limits/gap", float(data.run_epgap))
    if getattr(data, "run_epagap", 0) and float(data.run_epagap) > 0:
        model.setParam("limits/absgap", float(data.run_epagap))
    if getattr(data, "run_tilim", 0) and float(data.run_tilim) > 0:
        model.setParam("limits/time", float(data.run_tilim))

    model.setParam("display/verblevel", 4)

    t0 = time.time()
    model.optimize()
    runtime_sec = time.time() - t0

    status = str(model.getStatus())
    best_sol = model.getBestSol()
    print(f"[SCIP] Solver status: {status}")

    if best_sol is None:
        solution = Solution(
            status=status,
            objective_value=0.0,
            V_IDs=[0] * data.num_of_SKUs,
            B_IDs=[0] * data.num_of_SKUs,
            gap=0.0,
        )
        return solution, model, runtime_sec

    solution = extract_solution(model, x, data, best_sol)
    print(f"[SCIP] Best objective (bestSol): {solution.objective_value}")
    if solution.gap > 0:
        print(f"[SCIP] Final relative gap: {solution.gap}")

    return solution, model, runtime_sec


def _build_single_vehicle_data(base_data: ModelData, remaining_global_indices: list[int], remaining_time: float) -> ModelData:
    """
    从原始数据中抽取“剩余 SKU”，构造单车求解子数据。

    关键假设：
    1. 每一轮只求解 1 辆车，因此将 num_of_Vehicles 固定为 1；
    2. 每一轮至少形成 1 个批次，因此将 B_LB 固定为 1；
    3. 其余批次区间、每车最多批次数、每批最多 SKU 数、重量窗等参数保持不变。
    """
    subset_df = base_data.SKUs_Data.iloc[remaining_global_indices].reset_index(drop=True).copy()
    sub_num_skus = len(subset_df)
    beta, alpha, w, _w = preprocess_sku_data(subset_df, sub_num_skus)

    return replace(
        base_data,
        num_of_SKUs=sub_num_skus,
        num_of_Vehicles=1,
        I=range(sub_num_skus),
        V=range(1),
        B_LB=1,
        SKUs_Data=subset_df,
        beta=beta,
        alpha=alpha,
        w=w,
        _w=_w,
        run_tilim=max(0.0, remaining_time),
    )


def _resolve_single_vehicle_time_limit(remaining_global_time: float, vehicle_time_limit: float | None) -> float:
    """
    预留接口：计算单车子问题的时间上限。

    默认 vehicle_time_limit=None 时，保持原逻辑：
    - 若 Params!B12 > 0，则每车使用“全局剩余时间”；
    - 若 Params!B12 <= 0，则不设置单车 SCIP time limit。

    若传入 vehicle_time_limit > 0，则每辆车最多求解该秒数；
    如果同时设置了全局时间上限，则取 min(单车上限, 全局剩余时间)。
    """
    if vehicle_time_limit is None or float(vehicle_time_limit) <= 0:
        return max(0.0, float(remaining_global_time or 0.0))

    per_vehicle_limit = float(vehicle_time_limit)
    if remaining_global_time and float(remaining_global_time) > 0:
        return max(0.0, min(per_vehicle_limit, float(remaining_global_time)))
    return per_vehicle_limit


def _solve_sequential_vehicle_model(
    data: ModelData,
    input_file: str,
    output_file: str,
    vehicle_time_limit: float | None = None,
):
    """
    顺序配矿：
    输入 V 辆车，但不再一次性联立求解，而是按车辆顺序逐车求解。

    数学上，这是一种“逐车滚动优化”策略，而不是原始整体模型的等价重构。
    它通常更快，但不能保证得到“整体联立求解”下的全局最优解。
    """
    total_vehicle_num = int(data.num_of_Vehicles)
    original_num_skus = int(data.num_of_SKUs)
    total_time_limit = float(getattr(data, "run_tilim", 0) or 0)

    global_V_IDs = [0] * original_num_skus
    global_B_IDs = [0] * original_num_skus
    remaining_global_indices = list(range(original_num_skus))

    total_obj = 0.0
    total_runtime = 0.0
    last_model = None

    print("构建顺序配矿模型（按车辆逐车求解，不再一次性联立求解）...")

    for vehicle_id in range(1, total_vehicle_num + 1):
        print(f"\n================ 当前求解车辆 {vehicle_id}/{total_vehicle_num} ================")

        if len(remaining_global_indices) == 0:
            print(f"[error]在求解第 {vehicle_id} 车前，剩余 SKU 已为空，无法继续配矿。")
            return Solution(
                status="infeasible",
                objective_value=total_obj,
                V_IDs=global_V_IDs,
                B_IDs=global_B_IDs,
                gap=0.0,
            ), None, last_model, None

        remaining_wet_weight = sum(float(data.w[i]) for i in remaining_global_indices)
        if remaining_wet_weight + 1e-9 < float(data.W_v_min):
            print(
                f"[error]在求解第 {vehicle_id} 车前，剩余 SKU 总湿重={remaining_wet_weight:.6f}，"
                f"已小于单车最低装载量 W_v_min={float(data.W_v_min):.6f}，无法继续配矿。"
            )
            return Solution(
                status="infeasible",
                objective_value=total_obj,
                V_IDs=global_V_IDs,
                B_IDs=global_B_IDs,
                gap=0.0,
            ), None, last_model, None

        if total_time_limit > 0:
            remaining_time = total_time_limit - total_runtime
            if remaining_time <= 1e-9:
                print(f"[error]全局时间上限已耗尽，停止于第 {vehicle_id} 车之前。")
                return Solution(
                    status="timelimit",
                    objective_value=total_obj,
                    V_IDs=global_V_IDs,
                    B_IDs=global_B_IDs,
                    gap=0.0,
                ), None, last_model, None
        else:
            remaining_time = 0.0

        single_vehicle_time_limit = _resolve_single_vehicle_time_limit(remaining_time, vehicle_time_limit)
        if vehicle_time_limit is not None and float(vehicle_time_limit) > 0:
            print(f"[SEQ] 单车求解时间上限接口已启用：本车 time limit = {single_vehicle_time_limit:.3f} 秒")

        vehicle_data = _build_single_vehicle_data(data, remaining_global_indices, single_vehicle_time_limit)
        vehicle_solution, vehicle_model, vehicle_runtime = _solve_single_vehicle_mip(vehicle_data)
        total_runtime += vehicle_runtime
        last_model = vehicle_model

        if vehicle_solution.status in {"infeasible", "inforunbd", "unbounded"}:
            print(f"[error]第 {vehicle_id} 车单车模型不可行，顺序配矿终止。")
            return Solution(
                status="infeasible",
                objective_value=total_obj,
                V_IDs=global_V_IDs,
                B_IDs=global_B_IDs,
                gap=vehicle_solution.gap,
            ), None, last_model, None

        local_selected = []
        for local_i in range(vehicle_data.num_of_SKUs):
            if vehicle_solution.V_IDs[local_i] > 0:
                global_i = remaining_global_indices[local_i]
                global_V_IDs[global_i] = vehicle_id
                global_B_IDs[global_i] = vehicle_solution.B_IDs[local_i]
                local_selected.append(local_i)

        if not local_selected:
            print(f"[error]第 {vehicle_id} 车虽然模型返回 {vehicle_solution.status}，但未选中任何 SKU，顺序配矿终止。")
            return Solution(
                status="infeasible",
                objective_value=total_obj,
                V_IDs=global_V_IDs,
                B_IDs=global_B_IDs,
                gap=vehicle_solution.gap,
            ), None, last_model, None

        selected_global = {remaining_global_indices[local_i] for local_i in local_selected}
        remaining_global_indices = [idx for idx in remaining_global_indices if idx not in selected_global]
        total_obj += float(vehicle_solution.objective_value)

        loaded_weight = sum(float(data.w[i]) for i in selected_global)
        print(
            f"[SEQ] 第 {vehicle_id} 车完成：已分配 SKU 数={len(selected_global)}，"
            f"装载湿重={loaded_weight:.6f}，本车目标值={float(vehicle_solution.objective_value):.6f}，"
            f"剩余 SKU 数={len(remaining_global_indices)}"
        )

    final_solution = Solution(
        status="optimal",
        objective_value=total_obj,
        V_IDs=global_V_IDs,
        B_IDs=global_B_IDs,
        gap=0.0,
    )

    saved_file = save_solution(final_solution, data, input_file, output_file)
    print_solution_summary(final_solution, data, runtime_sec=total_runtime, saved_file=saved_file)
    return final_solution, saved_file, last_model, None


# ==========================
def solve_benders_model(
    data: ModelData,
    input_file: str,
    output_file: str,
    vehicle_time_limit: float | None = None,
):
    """
    为保持 main.py 调用接口不变，仍保留 solve_benders_model 这个函数名。

    但内部求解逻辑已修改为：
    - 输入 V 辆车；
    - 每次只构造 1 辆车的 MIP；
    - 求解完后删除已使用 SKU；
    - 再求下一辆车；
    - 直至所有车次完成或中途不可行。
    """
    return _solve_sequential_vehicle_model(
        data,
        input_file,
        output_file,
        vehicle_time_limit=vehicle_time_limit,
    )
