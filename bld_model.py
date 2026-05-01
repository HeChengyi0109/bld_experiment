from pyscipopt import Model, quicksum
from bld_data import ModelData


def build_mip_model(data: ModelData):
    """
    构建“单体 MIP”模型：
    方案A：data.K 已在读入阶段过滤到 Active=1 的批次集合
    """
    model = Model("BLD-MIP")
    I, V, K = data.I, data.V, data.K

    # 变量
    x = {}
    for i in I:
        for v in V:
            for k in K:
                x[i, v, k] = model.addVar(vtype="B", name=f"x_{i}_{v}_{k}")

    delta = {}
    for v in V:
        for k in K:
            delta[v, k] = model.addVar(vtype="B", name=f"delta_{v}_{k}")

    # --- 约束 1：每个 SKU 至多分配一次 ---
    for i in I:
        model.addCons(
            quicksum(x[i, v, k] for v in V for k in K) <= 1,
            name=f"ct_each_SKU_assigned_once_{i}",
        )

    # --- 约束 2：总批次数量下界 ---
    model.addCons(
        quicksum(delta[v, k] for v in V for k in K) >= int(data.B_LB),
        name="ct_LB_of_total_batches",
    )

    # --- 约束 3：每辆车启用批次数量上界 ---
    for v in V:
        model.addCons(
            quicksum(delta[v, k] for k in K) <= int(data.b_v),
            name=f"ct_UB_of_batches_per_vehicle_{v}",
        )

    # --- 约束 4：每辆车总湿重上下界 ---
    for v in V:
        model.addCons(
            quicksum(data.w[i] * x[i, v, k] for i in I for k in K) >= float(data.W_v_min),
            name=f"ct_weight_LB_of_vehicle_{v}",
        )
        model.addCons(
            quicksum(data.w[i] * x[i, v, k] for i in I for k in K) <= float(data.W_v_max),
            name=f"ct_weight_UB_of_vehicle_{v}",
        )

    # --- 约束 5：批次逻辑约束 ---
    for v in V:
        for k in K:
            model.addCons(
                quicksum(x[i, v, k] for i in I) <= int(data.M_vk) * delta[v, k],
                name=f"ct_UB_logic_x_and_delta_{v}_{k}",
            )

    # --- 约束 6：Mo 品位区间（Big-M 开关） ---
    for v in V:
        for k in K:
            lhs = quicksum(data.beta[i] * data._w[i] * x[i, v, k] for i in I)
            denom = quicksum(data._w[i] * x[i, v, k] for i in I)

            model.addCons(
                lhs >= float(data.beta_min[k]) * denom - float(data.M_Mo) * (1 - delta[v, k]),
                name=f"ct_LB_Mo_grade_{v}_{k}",
            )
            model.addCons(
                lhs <= float(data.beta_max[k]) * denom + float(data.M_Mo) * (1 - delta[v, k]),
                name=f"ct_UB_Mo_grade_{v}_{k}",
            )

    # --- 约束 7：Cu 品位区间（Big-M 开关） ---
    for v in V:
        for k in K:
            lhs = quicksum(data.alpha[i] * data._w[i] * x[i, v, k] for i in I)
            denom = quicksum(data._w[i] * x[i, v, k] for i in I)

            model.addCons(
                lhs <= float(data.alpha_max[k]) * denom + float(data.M_Cu) * (1 - delta[v, k]),
                name=f"ct_UB_Cu_grade_{v}_{k}",
            )
            model.addCons(
                lhs >= float(data.alpha_min[k]) * denom - float(data.M_Cu) * (1 - delta[v, k]),
                name=f"ct_LB_Cu_grade_{v}_{k}",
            )

    # 目标：最大化利润
    obj = quicksum(
        float(data.a[k]) * float(data.beta[i]) * float(data._w[i]) * x[i, v, k]
        for i in I for v in V for k in K
    )
    model.setObjective(obj, "maximize")

    model.data = {"x": x, "delta": delta}
    return model
