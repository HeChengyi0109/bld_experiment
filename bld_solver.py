from __future__ import annotations

"""
bld_solver.py
1) 保留 SCIP 直接求解单体 MIP 的能力；
2) 新增一个“硬约束与完整模型一致”的 greedy 启发式：
   - 每个 SKU 至多分配一次；
   - 每辆车总湿重必须落在 [W_v_min, W_v_max]；
   - 每辆车最多启用 b_v 个批次；
   - 每个 (v,k) 最多分配 M_vk 个 SKU；
   - 每个非空 (v,k) 的 Cu/Mo 混配品位必须落在批次 k 的区间内；
   - 总激活批次数下界 B_LB 若由非空批次不足，则用“空激活 delta”补齐。

说明：
- 由于当前 Solution 结构只保存 x 对应的 V_ID/B_ID，而不显式保存 delta，
  因此 greedy 导出的 Excel 只展示“非空批次”；
- 但在可行性判定时，代码会额外检查：是否可以用空批次把 delta 数量补到 B_LB。
"""

import time
from typing import Dict, List, Optional, Sequence, Set, Tuple

import pandas as pd
from pyscipopt import SCIP_PARAMSETTING

from bld_data import ModelData, Solution
from bld_model import build_mip_model
from bld_utils import write_df_to_sheet


# ==========================
# 原 SCIP 结果提取/保存函数
# ==========================

def extract_solution(model, x, data: ModelData, best_sol) -> Solution:
    """从 best_sol 提取最终解（适用于 optimal / timelimit / gaplimit 等情况）"""
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
    """保存求解结果到 output.xlsx，覆盖写 SKUs 与 VBResult。"""
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
    pair_to_idx: Dict[Tuple[int, int], List[int]] = {}
    for i in range(data.num_of_SKUs):
        v = int(solution.V_IDs[i])
        b = int(solution.B_IDs[i])
        if v <= 0 or b <= 0:
            continue
        pair_to_idx.setdefault((v, b), []).append(i)

    for (v, b), idx in sorted(pair_to_idx.items(), key=lambda x: (x[0][0], x[0][1])):
        W = sum(data.w[i] for i in idx)
        Wdry = sum(data._w[i] for i in idx)
        mo_metal = 100.0 * sum(data.beta[i] * data._w[i] for i in idx)
        cu_metal = 100.0 * sum(data.alpha[i] * data._w[i] for i in idx)
        mo_grade = (mo_metal / Wdry) if Wdry > 0 else 0.0
        cu_grade = (cu_metal / Wdry) if Wdry > 0 else 0.0
        moisture = ((W - Wdry) / W) if W > 0 else 0.0
        unit_price = float(data.a[b - 1])
        total_price = unit_price * mo_metal

        rows.append({
            "V_ID": v,
            "B_ID": b,
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
    runtime_sec: Optional[float] = None,
    saved_file: Optional[str] = None,
    method_name: str = "scip",
):
    """打印求解摘要。"""

    def _fmt(x: float) -> str:
        try:
            return f"{float(x):.6g}"
        except Exception:
            return str(x)

    print("\n=== 求解结果摘要 ===")
    print(f"[result]方法：{method_name}")
    print(f"[result]运行时间：{'N/A' if runtime_sec is None else f'{runtime_sec:.3f} s'}")
    print(f"[result]求解状态：{solution.status}")
    print(f"[result]目标值：{solution.objective_value:.6f}" if solution.objective_value is not None else "[result]目标值：N/A")

    assigned_pairs = set()
    used_batches = set()
    used_vehicles = set()
    assigned_skus = 0
    for i in range(data.num_of_SKUs):
        v = int(solution.V_IDs[i])
        b = int(solution.B_IDs[i])
        if v > 0:
            assigned_skus += 1
            used_vehicles.add(v)
        if v > 0 and b > 0:
            assigned_pairs.add((v, b))
            used_batches.add(b)

    print(f"[result]使用批次数量：{len(assigned_pairs)}")

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

    unmatched_assigned = sum(1 for i in range(data.num_of_SKUs) if int(solution.V_IDs[i]) > 0 and int(solution.B_IDs[i]) <= 0)
    unassigned = sum(1 for i in range(data.num_of_SKUs) if int(solution.V_IDs[i]) <= 0)
    if unmatched_assigned > 0:
        print(f"[warn]有 {unmatched_assigned} 个已分配 SKU 所在车辆混配后未落入任何已定义批次区间。")
    if unassigned > 0:
        print(f"[warn]有 {unassigned} 个 SKU 未被分配到任何车辆。")


# ==========================
# 通用数值工具
# ==========================

def _safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default)



def _blend_stats(data: ModelData, sku_ids: Sequence[int]) -> Dict[str, float]:
    """计算一组 SKU 的混配统计量。返回的 Cu/Mo grade 单位为“百分数”。"""
    W = sum(data.w[i] for i in sku_ids)
    Wdry = sum(data._w[i] for i in sku_ids)
    mo_metal = 100.0 * sum(data.beta[i] * data._w[i] for i in sku_ids)
    cu_metal = 100.0 * sum(data.alpha[i] * data._w[i] for i in sku_ids)
    mo_grade = (mo_metal / Wdry) if Wdry > 1e-12 else 0.0
    cu_grade = (cu_metal / Wdry) if Wdry > 1e-12 else 0.0
    moisture = ((W - Wdry) / W) if W > 1e-12 else 0.0
    return {
        "W": W,
        "Wdry": Wdry,
        "Mo metal": mo_metal,
        "Cu metal": cu_metal,
        "Mo grade": mo_grade,
        "Cu grade": cu_grade,
        "moisture": moisture,
    }



def _batch_bounds_pct(data: ModelData, k: int) -> Tuple[float, float, float, float]:
    mo_lo = 100.0 * _safe_float(data.beta_min[k])
    mo_hi = 100.0 * _safe_float(data.beta_max[k])
    cu_lo = 100.0 * _safe_float(data.alpha_min[k])
    cu_hi = 100.0 * _safe_float(data.alpha_max[k])
    return mo_lo, mo_hi, cu_lo, cu_hi



def _batch_violation(data: ModelData, mo_grade_pct: float, cu_grade_pct: float, k: int) -> float:
    mo_lo, mo_hi, cu_lo, cu_hi = _batch_bounds_pct(data, k)
    mo_scale = max(mo_hi - mo_lo, 1e-8)
    cu_scale = max(cu_hi - cu_lo, 1e-8)

    if mo_grade_pct < mo_lo:
        mo_v = (mo_lo - mo_grade_pct) / mo_scale
    elif mo_grade_pct > mo_hi:
        mo_v = (mo_grade_pct - mo_hi) / mo_scale
    else:
        mo_v = 0.0

    if cu_grade_pct < cu_lo:
        cu_v = (cu_lo - cu_grade_pct) / cu_scale
    elif cu_grade_pct > cu_hi:
        cu_v = (cu_grade_pct - cu_hi) / cu_scale
    else:
        cu_v = 0.0

    return mo_v + cu_v



def _is_exact_in_batch(data: ModelData, sku_ids: Sequence[int], k: int) -> bool:
    if not sku_ids:
        return False
    st = _blend_stats(data, sku_ids)
    return _batch_violation(data, st["Mo grade"], st["Cu grade"], k) <= 1e-12



def _batch_revenue(data: ModelData, k: int, sku_ids: Sequence[int]) -> float:
    if not sku_ids:
        return 0.0
    st = _blend_stats(data, sku_ids)
    return float(data.a[k]) * st["Mo metal"]



def _individual_violation(data: ModelData, i: int, k: int) -> float:
    mo = 100.0 * _safe_float(data.beta[i])
    cu = 100.0 * _safe_float(data.alpha[i])
    return _batch_violation(data, mo, cu, k)



def _find_seed_exact_batch(
    data: ModelData,
    remaining: Sequence[int],
    k: int,
    cap_weight: float,
    max_count: int,
) -> List[int]:
    """
    为固定批次 k 寻找一个“精确落区”的初始种子。
    依次尝试：单 SKU、两 SKU、三 SKU（限制候选池大小）。
    """
    cand = [i for i in remaining if data.w[i] <= cap_weight + 1e-9]
    if not cand or max_count <= 0:
        return []

    cand = sorted(
        cand,
        key=lambda i: (
            _individual_violation(data, i, k),
            -float(data.a[k]) * float(data.beta[i]) * float(data._w[i]),
            -float(data.w[i]),
        )
    )

    # 1) 单 SKU
    for i in cand:
        if _is_exact_in_batch(data, [i], k):
            return [i]

    # 2) 两 SKU
    top2 = cand[: min(len(cand), 30)]
    if max_count >= 2:
        best_pair: List[int] = []
        best_key = None
        for a_idx in range(len(top2)):
            i = top2[a_idx]
            for b_idx in range(a_idx + 1, len(top2)):
                j = top2[b_idx]
                if data.w[i] + data.w[j] > cap_weight + 1e-9:
                    continue
                trial = [i, j]
                if _is_exact_in_batch(data, trial, k):
                    rev = _batch_revenue(data, k, trial)
                    key = (rev, data.w[i] + data.w[j])
                    if best_key is None or key > best_key:
                        best_key = key
                        best_pair = trial
        if best_pair:
            return best_pair

    # 3) 三 SKU
    top3 = cand[: min(len(cand), 12)]
    if max_count >= 3:
        best_triple: List[int] = []
        best_key = None
        for a_idx in range(len(top3)):
            i = top3[a_idx]
            for b_idx in range(a_idx + 1, len(top3)):
                j = top3[b_idx]
                w2 = data.w[i] + data.w[j]
                if w2 > cap_weight + 1e-9:
                    continue
                for c_idx in range(b_idx + 1, len(top3)):
                    h = top3[c_idx]
                    if w2 + data.w[h] > cap_weight + 1e-9:
                        continue
                    trial = [i, j, h]
                    if _is_exact_in_batch(data, trial, k):
                        rev = _batch_revenue(data, k, trial)
                        key = (rev, w2 + data.w[h])
                        if best_key is None or key > best_key:
                            best_key = key
                            best_triple = trial
        if best_triple:
            return best_triple

    return []



def _extend_exact_batch(
    data: ModelData,
    remaining: Set[int],
    k: int,
    seed: Sequence[int],
    cap_weight: float,
    max_count: int,
    target_gap: float,
) -> List[int]:
    """
    在已精确落区的 seed 基础上，继续加入 SKU，要求加入后仍精确落区，
    且不超过 cap_weight 与 max_count。优先补重量，再兼顾收益。
    """
    selected = list(seed)
    if not selected:
        return []

    while len(selected) < max_count:
        best_j = None
        best_key = None
        cur_w = sum(data.w[i] for i in selected)
        for j in remaining:
            if j in selected:
                continue
            if cur_w + data.w[j] > cap_weight + 1e-9:
                continue
            trial = selected + [j]
            if not _is_exact_in_batch(data, trial, k):
                continue
            st = _blend_stats(data, trial)
            rev = float(data.a[k]) * st["Mo metal"]
            weight_fill = min(st["W"], max(target_gap, 0.0))
            key = (weight_fill, rev, st["W"])
            if best_key is None or key > best_key:
                best_key = key
                best_j = j
        if best_j is None:
            break
        selected.append(best_j)

    return selected



def _construct_exact_batch_for_k(
    data: ModelData,
    remaining: Set[int],
    k: int,
    cap_weight: float,
    max_count: int,
    target_gap: float,
) -> List[int]:
    """
    在剩余 SKU 中为批次 k 构造一个精确落区的非空批次。
    返回的批次一定满足：
    - 1 <= |S| <= max_count
    - sum_w(S) <= cap_weight
    - 混配 Cu/Mo 精确落在 k 区间内
    """
    seed = _find_seed_exact_batch(data, list(remaining), k, cap_weight, max_count)
    if not seed:
        return []
    batch = _extend_exact_batch(data, remaining, k, seed, cap_weight, max_count, target_gap)
    if not _is_exact_in_batch(data, batch, k):
        return []
    return batch



def _construct_one_vehicle_hard(
    data: ModelData,
    remaining: Set[int],
    vehicle_index: int,
) -> Tuple[bool, Dict[int, List[int]], float]:
    """
    为第 vehicle_index 辆车构造一个满足完整模型硬约束的车辆装载方案。

    返回：
        ok, batch_map, total_weight
    其中 batch_map 的键为 0-based k，值为该 (v,k) 的 SKU 列表。
    """
    if not remaining:
        return False, {}, 0.0

    batches: Dict[int, List[int]] = {}
    total_w = 0.0
    used_k: Set[int] = set()

    future_vehicle_cnt = data.num_of_Vehicles - vehicle_index - 1

    while len(batches) < int(data.b_v):
        remaining_total_weight = sum(data.w[i] for i in remaining)
        reserve_future = future_vehicle_cnt * float(data.W_v_min)
        if remaining_total_weight < reserve_future - 1e-9:
            return False, {}, 0.0

        cap_by_vehicle = float(data.W_v_max) - total_w
        if cap_by_vehicle <= 1e-9:
            break

        target_gap = max(float(data.W_v_min) - total_w, 0.0)

        best_k = None
        best_batch: List[int] = []
        best_key = None

        for k in data.K:
            if k in used_k:
                continue

            batch = _construct_exact_batch_for_k(
                data=data,
                remaining=remaining,
                k=k,
                cap_weight=cap_by_vehicle,
                max_count=int(data.M_vk),
                target_gap=target_gap,
            )
            if not batch:
                continue

            batch_w = sum(data.w[i] for i in batch)
            if batch_w > cap_by_vehicle + 1e-9:
                continue

            # 选完该批次后，未来车辆仍需至少 reserve_future 的重量
            if remaining_total_weight - batch_w < reserve_future - 1e-9:
                continue

            rev = _batch_revenue(data, k, batch)
            new_total_w = total_w + batch_w
            reach_flag = 1 if new_total_w >= float(data.W_v_min) - 1e-9 else 0
            fill_ratio = min(new_total_w, float(data.W_v_min)) / max(float(data.W_v_min), 1e-9)
            key = (reach_flag, fill_ratio, rev, batch_w)

            if best_key is None or key > best_key:
                best_key = key
                best_k = k
                best_batch = batch

        if best_k is None:
            break

        batches[best_k] = list(best_batch)
        used_k.add(best_k)
        total_w += sum(data.w[i] for i in best_batch)
        remaining.difference_update(best_batch)

        # 一旦当前车辆达到重量下界，即停止构造该车；
        # 这样更稳妥，避免前车吃掉过多重量导致后车 infeasible。
        if total_w >= float(data.W_v_min) - 1e-9:
            break

    ok = (float(data.W_v_min) - 1e-9 <= total_w <= float(data.W_v_max) + 1e-9)
    if not ok:
        return False, {}, 0.0

    return True, batches, total_w



def _complete_active_slots(
    used_vk: Set[Tuple[int, int]],
    data: ModelData,
) -> Optional[Set[Tuple[int, int]]]:
    """
    若非空批次数不足 B_LB，则利用“空激活批次”补齐。
    这与当前单体 MIP 完整模型一致，因为模型中没有 delta 的下链接下界。
    """
    target = int(data.B_LB)
    active = set(used_vk)
    if len(active) >= target:
        return active

    for v in data.V:
        current_v_cnt = sum(1 for (vv, _) in active if vv == v)
        room = int(data.b_v) - current_v_cnt
        if room <= 0:
            continue
        for k in data.K:
            if (v, k) in active:
                continue
            active.add((v, k))
            room -= 1
            if len(active) >= target:
                return active
            if room <= 0:
                break

    return active if len(active) >= target else None



def construct_hard_greedy_solution(data: ModelData) -> Tuple[Solution, Dict[str, object]]:
    """
    构造一个满足完整模型硬约束的 greedy 解（若成功）。

    返回：
        solution, info
    其中 info 包括：
        - used_vk_nonempty: 非空批次集合
        - active_vk: 补齐 B_LB 后的激活批次集合（可含空批次）
        - empty_delta_cnt: 为满足 B_LB 而额外补的空批次数量
    """
    remaining: Set[int] = set(data.I)
    V_IDs = [0] * data.num_of_SKUs
    B_IDs = [0] * data.num_of_SKUs
    used_vk_nonempty: Set[Tuple[int, int]] = set()

    total_remaining_weight = sum(data.w[i] for i in remaining)
    if total_remaining_weight < data.num_of_Vehicles * float(data.W_v_min) - 1e-9:
        sol = Solution("infeasible", 0.0, V_IDs, B_IDs, 0.0)
        return sol, {
            "reason": "总湿重不足以支撑所有车辆达到重量下界。",
            "used_vk_nonempty": set(),
            "active_vk": None,
            "empty_delta_cnt": None,
        }

    for v in data.V:
        ok, batch_map, total_w = _construct_one_vehicle_hard(data, remaining, v)
        if not ok:
            sol = Solution("infeasible", 0.0, V_IDs, B_IDs, 0.0)
            return sol, {
                "reason": f"第 {v + 1} 车无法在 b_v={data.b_v}, M_vk={data.M_vk} 以及品位约束下构造到重量窗内。",
                "used_vk_nonempty": used_vk_nonempty,
                "active_vk": None,
                "empty_delta_cnt": None,
            }

        vehicle_total_w = 0.0
        for k, sku_list in batch_map.items():
            used_vk_nonempty.add((v, k))
            for i in sku_list:
                V_IDs[i] = v + 1
                B_IDs[i] = k + 1
                vehicle_total_w += data.w[i]

        if not (float(data.W_v_min) - 1e-9 <= vehicle_total_w <= float(data.W_v_max) + 1e-9):
            sol = Solution("infeasible", 0.0, V_IDs, B_IDs, 0.0)
            return sol, {
                "reason": f"第 {v + 1} 车最终重量 {vehicle_total_w:.6f} 不在 [{data.W_v_min}, {data.W_v_max}] 内。",
                "used_vk_nonempty": used_vk_nonempty,
                "active_vk": None,
                "empty_delta_cnt": None,
            }

    active_vk = _complete_active_slots(used_vk_nonempty, data)
    if active_vk is None:
        sol = Solution("infeasible", 0.0, V_IDs, B_IDs, 0.0)
        return sol, {
            "reason": "非空批次与可补空批次之和仍无法满足 B_LB。",
            "used_vk_nonempty": used_vk_nonempty,
            "active_vk": None,
            "empty_delta_cnt": None,
        }

    # 目标值：与完整模型一致，只按 x 计算收益，不对空批次计收益。
    objective_value = 0.0
    for i in data.I:
        v_id = int(V_IDs[i])
        b_id = int(B_IDs[i])
        if v_id > 0 and b_id > 0:
            objective_value += float(data.a[b_id - 1]) * float(data.beta[i]) * float(data._w[i])

    sol = Solution(
        status="heuristic",
        objective_value=objective_value,
        V_IDs=V_IDs,
        B_IDs=B_IDs,
        gap=0.0,
    )
    return sol, {
        "reason": "ok",
        "used_vk_nonempty": used_vk_nonempty,
        "active_vk": active_vk,
        "empty_delta_cnt": len(active_vk) - len(used_vk_nonempty),
    }


# ==========================
# 原 SCIP 单体 MIP 求解
# ==========================

def solve_benders_model(data: ModelData, input_file: str, output_file: str):
    """
    兼容原 main.py 的调用名：仍叫 solve_benders_model，
    但内部为“直接用 SCIP 求解单体 MIP”。
    """
    print("构建单体 MIP 模型（不使用 Benders）...")
    model = build_mip_model(data)
    x = model.data["x"]

    print("配置 SCIP 求解参数 ...")
    try:
        model.setPresolve(SCIP_PARAMSETTING.OFF)
    except Exception:
        pass

    if getattr(data, "run_epgap", 0) and float(data.run_epgap) > 0:
        model.setParam("limits/gap", float(data.run_epgap))
    if getattr(data, "run_epagap", 0) and float(data.run_epagap) > 0:
        model.setParam("limits/absgap", float(data.run_epagap))
    if getattr(data, "run_tilim", 0) and float(data.run_tilim) > 0:
        model.setParam("limits/time", float(data.run_tilim))
    model.setParam("display/verblevel", 4)

    print("开始求解 MIP ...")
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
        print("[error]在时间限制内未找到任何可行解。不生成输出文件。")
        return solution, None, model, None

    solution = extract_solution(model, x, data, best_sol)
    saved_file = save_solution(solution, data, input_file, output_file)
    print_solution_summary(solution, data, runtime_sec=runtime_sec, saved_file=saved_file, method_name="scip")
    return solution, saved_file, model, None


# ==========================
# greedy 启发式（硬约束版）
# ==========================

def solve_greedy_heuristic(data: ModelData, input_file: str, output_file: str):
    """
    greedy 启发式：硬约束与完整模型保持一致。
    若无法构造完整可行解，则返回 infeasible。
    """
    print("开始执行 greedy 启发式（硬约束与完整模型一致）...")
    t0 = time.time()

    solution, info = construct_hard_greedy_solution(data)
    runtime_sec = time.time() - t0

    if solution.status != "heuristic":
        print(f"[greedy][error]{info.get('reason', '未知原因')}" )
        print_solution_summary(solution, data, runtime_sec=runtime_sec, method_name="greedy-hard")
        return solution, None, None, None

    saved_file = save_solution(solution, data, input_file, output_file)
    print_solution_summary(solution, data, runtime_sec=runtime_sec, saved_file=saved_file, method_name="greedy-hard")
    print(f"[greedy-hard]非空批次数：{len(info['used_vk_nonempty'])}")
    print(f"[greedy-hard]为满足 B_LB 额外补的空批次数：{info['empty_delta_cnt']}")
    return solution, saved_file, None, None
