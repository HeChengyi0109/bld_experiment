"""
数据读取模块 - 负责从Excel文件读取和处理数据
（方案A：在读取阶段过滤 Active=0 的批次，使模型 K 直接变小）
"""
import pandas as pd
from dataclasses import dataclass
from typing import List, Union, Optional, Dict
from common import read_cell_value


@dataclass
class ModelData:
    """模型数据结构"""
    num_of_SKUs: int
    num_of_Vehicles: int
    num_of_Batches: int
    I: range
    V: range
    K: range
    aK: range
    B_LB: int
    B_UB: int
    b_v: int
    M_vk: int
    W_v_min: float
    W_v_max: float
    SKUs_Data: pd.DataFrame
    beta: List[float]
    alpha: List[float]
    w: List[float]
    _w: List[float]
    p: List[float]
    beta_min: Union[List[float], float]
    beta_max: Union[List[float], float]
    alpha_min: Union[List[float], float]
    alpha_max: Union[List[float], float]
    a: Union[List[float], float]
    M_Mo: float
    M_Cu: float
    run_epagap: float
    run_epgap: float
    run_tilim: float
    cmdstring_output_V_IDs: Optional[str]
    cmdstring_output_B_IDs: Optional[str]

    # 原始批次信息（未过滤前）
    original_num_of_Batches: Optional[int] = None
    # 过滤后批次在原 Prices 表中的行索引（0-based），用于你若要回写/对齐旧编号
    k_new_to_old: Optional[List[int]] = None


@dataclass
class Solution:
    """求解结果结构"""
    status: str
    objective_value: float
    V_IDs: List[int]
    B_IDs: List[int]
    gap: float = 0.0


def _normalize_cols(cols) -> List[str]:
    return [str(c).strip().upper() for c in cols]


def _find_col(colmap: Dict[str, str], *candidates: str) -> Optional[str]:
    for cand in candidates:
        key = str(cand).strip().upper()
        if key in colmap:
            return colmap[key]
    return None


def read_inv_skus_by_header(file_path: str, num_of_SKUs: int) -> pd.DataFrame:
    """
    按表头读取 inv_SKUs，并标准化为旧流程需要的 8 列结构。
    输出列顺序：
    [SKU_ID, V_ID, B_ID, weight, sale_weight, Cu_grade, Mo_grade, water_percentage]
    """
    df = pd.read_excel(file_path, sheet_name="inv_SKUs")
    if df is None or df.empty:
        raise ValueError("inv_SKUs sheet is empty.")

    norm = _normalize_cols(df.columns)
    colmap = {n: o for n, o in zip(norm, df.columns)}

    col_no = _find_col(colmap, "NO", "SKU_ID", "ID")
    col_qty = _find_col(colmap, "QUANTITY", "WEIGHT", "WET_WEIGHT", "W")
    col_ship = _find_col(colmap, "SHIPPED_QUANTITY", "SHIPPED", "SOLD",
                         "SALE_QUANTITY", "SALE_WEIGHT", "SHIPPED_WEIGHT")
    col_cu = _find_col(colmap, "INIT_CU_GRADE", "CU_GRADE", "CU", "INIT_CU")
    col_mo = _find_col(colmap, "INIT_MO_GRADE", "MO_GRADE", "MO", "INIT_MO")
    col_water = _find_col(colmap, "INIT_WATER", "WATER", "WATER_PERCENTAGE", "WATER_PCT", "MOISTURE")

    missing = [name for name, col in [
        ("NO", col_no),
        ("QUANTITY", col_qty),
        ("INIT_CU_GRADE", col_cu),
        ("INIT_MO_GRADE", col_mo),
        ("INIT_WATER", col_water),
    ] if col is None]
    if missing:
        raise ValueError(f"inv_SKUs missing required columns: {missing}. Found columns: {list(df.columns)}")

    out = pd.DataFrame()
    out["SKU_ID"] = df[col_no]
    out["V_ID"] = 0
    out["B_ID"] = 0
    out["weight"] = df[col_qty]
    out["sale_weight"] = df[col_ship] if col_ship is not None else 0
    out["Cu_grade"] = df[col_cu]
    out["Mo_grade"] = df[col_mo]
    out["water_percentage"] = df[col_water]

    for c in ["weight", "sale_weight", "Cu_grade", "Mo_grade", "water_percentage"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")

    out = out.dropna(subset=["weight"]).reset_index(drop=True)
    out = out.iloc[:int(num_of_SKUs)].reset_index(drop=True)

    if len(out) < int(num_of_SKUs):
        raise ValueError(f"inv_SKUs有效行数不足：需要 {num_of_SKUs} 行，但只读取到 {len(out)} 行。")

    return out


def preprocess_sku_data(SKUs_Data: pd.DataFrame, num_of_SKUs: int):
    """预处理SKU数据，计算品位与重量"""
    beta: List[float] = []
    alpha: List[float] = []
    w: List[float] = []
    _w: List[float] = []

    use_named = all(col in SKUs_Data.columns for col in ["Mo_grade", "Cu_grade", "weight", "water_percentage"])

    for i in range(int(num_of_SKUs)):
        if use_named:
            beta_i = float(SKUs_Data.loc[i, "Mo_grade"]) / 100.0
            alpha_i = float(SKUs_Data.loc[i, "Cu_grade"]) / 100.0
            w_i = float(SKUs_Data.loc[i, "weight"])
            water_pct = float(SKUs_Data.loc[i, "water_percentage"])
        else:
            # 兼容旧 iloc（不建议再用）
            beta_i = float(SKUs_Data.iloc[i, 6]) / 100.0
            alpha_i = float(SKUs_Data.iloc[i, 5]) / 100.0
            w_i = float(SKUs_Data.iloc[i, 3])
            water_pct = float(SKUs_Data.iloc[i, 7])

        _w_i = w_i * (1.0 - water_pct / 100.0)

        beta.append(beta_i)
        alpha.append(alpha_i)
        w.append(w_i)
        _w.append(_w_i)

    return beta, alpha, w, _w


def preprocess_grade_boundaries(beta_min, beta_max, alpha_min, alpha_max):
    """品位边界从百分数转为小数"""
    if isinstance(beta_min, list):
        beta_min = [x / 100.0 for x in beta_min]
    else:
        beta_min = beta_min / 100.0

    if isinstance(beta_max, list):
        beta_max = [x / 100.0 for x in beta_max]
    else:
        beta_max = beta_max / 100.0

    if isinstance(alpha_min, list):
        alpha_min = [x / 100.0 for x in alpha_min]
    else:
        alpha_min = alpha_min / 100.0

    if isinstance(alpha_max, list):
        alpha_max = [x / 100.0 for x in alpha_max]
    else:
        alpha_max = alpha_max / 100.0

    return beta_min, beta_max, alpha_min, alpha_max


def print_params_readback(params: dict):
    """Params sheet 读取回显"""
    print("\n========== Params 读取回显（input.xlsx -> Params） ==========")
    order = [
        ("num_of_SKUs", "B1"),
        ("num_of_Vehicles", "B2"),
        ("num_of_Batches", "B3"),
        ("B_LB", "B4"),
        ("B_UB", "B5"),
        ("b_v", "B6"),
        ("M_vk", "B7"),
        ("W_v_min", "B8"),
        ("W_v_max", "B9"),
        ("run_epagap", "B10"),
        ("run_epgap", "B11"),
        ("run_tilim", "B12"),
    ]
    for key, cell in order:
        val = params.get(key, None)
        print(f"{cell:>4} | {key:<15} = {val!r}")
    print("===========================================================\n")


def read_excel_data(file_path: str):
    """
    读取 Excel 数据（方案A）：
    - Prices 先读全表 -> 得到 Active -> 过滤 Active=1 行 -> 形成新的 K（更小）
    - 保留 k_new_to_old 映射，方便你如果要按原行号回写
    """
    try:
        # Params（仍读取，但批次数以 Prices 有效行数为准，然后再过滤）
        num_of_SKUs = int(read_cell_value(file_path, "Params", "B1"))
        num_of_Vehicles = int(read_cell_value(file_path, "Params", "B2"))
        num_of_Batches_param = int(read_cell_value(file_path, "Params", "B3"))

        B_LB = int(read_cell_value(file_path, "Params", "B4"))
        B_UB = int(read_cell_value(file_path, "Params", "B5"))
        b_v = int(read_cell_value(file_path, "Params", "B6"))
        M_vk = int(read_cell_value(file_path, "Params", "B7"))
        W_v_min = float(read_cell_value(file_path, "Params", "B8"))
        W_v_max = float(read_cell_value(file_path, "Params", "B9"))
        run_epagap = float(read_cell_value(file_path, "Params", "B10"))
        run_epgap = float(read_cell_value(file_path, "Params", "B11"))
        run_tilim = float(read_cell_value(file_path, "Params", "B12"))

        # Prices 读取（全量）
        df_prices = pd.read_excel(file_path, sheet_name="Prices")
        if df_prices is None or df_prices.empty:
            raise ValueError("Prices sheet is empty.")

        df_prices.columns = [str(c).strip() for c in df_prices.columns]

        # 仅保留有效数据行（以 Mo_min 非空判断）
        if "Mo_min" in df_prices.columns:
            df_prices = df_prices.dropna(subset=["Mo_min"]).reset_index(drop=True)

        original_num_of_Batches = int(len(df_prices))
        if num_of_Batches_param != original_num_of_Batches:
            print(
                f"[warn] Params!B3 批次数={num_of_Batches_param}，但 Prices 有效行数={original_num_of_Batches}。"
                f" 将以 Prices 行数为准。"
            )

        required_cols = ["Mo_min", "Mo_max", "Cu_min", "Cu_max", "Prices"]
        missing_cols = [c for c in required_cols if c not in df_prices.columns]
        if missing_cols:
            raise ValueError(f"[error]Prices sheet missing required columns: {missing_cols}. Found: {list(df_prices.columns)}")

        # Active：缺失则默认全启用
        if "Active" in df_prices.columns:
            active_raw = [int(x) if pd.notna(x) else 0 for x in df_prices["Active"].tolist()]
        else:
            active_raw = [1] * original_num_of_Batches

        # ===== 方案A：过滤 Active=0 批次 =====
        k_new_to_old = [k for k in range(original_num_of_Batches) if int(active_raw[k]) == 1]
        filtered_cnt = len(k_new_to_old)

        if filtered_cnt == 0:
            raise ValueError("[error]没有任何启用的批次，模型无法构建。")

        if B_LB > filtered_cnt * num_of_Vehicles:
            # 这是逻辑上必不可行的硬条件（总可用 delta 个数不足）
            raise ValueError(
                f"[error]不可行：B_LB={B_LB}，但可用批次(Active=1)={filtered_cnt}，车辆数={num_of_Vehicles}，"
                f"最多可启用批次数={filtered_cnt * num_of_Vehicles}。请降低 B_LB 或增加启用批次。"
            )

        df_prices_f = df_prices.iloc[k_new_to_old].reset_index(drop=True)

        # 过滤后的参数数组（长度=filtered_cnt）
        beta_min = df_prices_f["Mo_min"].tolist()
        beta_max = df_prices_f["Mo_max"].tolist()
        alpha_min = df_prices_f["Cu_min"].tolist()
        alpha_max = df_prices_f["Cu_max"].tolist()
        a = df_prices_f["Prices"].tolist()

        # inv_SKUs
        SKUs_Data = read_inv_skus_by_header(file_path, num_of_SKUs)

        # 回显 Params
        params_rb = {
            "num_of_SKUs": num_of_SKUs,
            "num_of_Vehicles": num_of_Vehicles,
            "num_of_Batches": original_num_of_Batches,  # 注意这里回显的是 Prices 原始行数
            "B_LB": B_LB,
            "B_UB": B_UB,
            "b_v": b_v,
            "M_vk": M_vk,
            "W_v_min": W_v_min,
            "W_v_max": W_v_max,
            "run_epagap": run_epagap,
            "run_epgap": run_epgap,
            "run_tilim": run_tilim,
        }
        print_params_readback(params_rb)

        print("数据读取成功!")
        print(
            f"Prices 原始批次数: {original_num_of_Batches}；启用批次数(进入模型): {filtered_cnt}；"
            f"Active=0 已被过滤: {original_num_of_Batches - filtered_cnt}"
        )

        # 预处理 SKU
        beta, alpha, w, _w = preprocess_sku_data(SKUs_Data, num_of_SKUs)

        # 品位边界百分数转小数（过滤后再转）
        beta_min, beta_max, alpha_min, alpha_max = preprocess_grade_boundaries(
            beta_min, beta_max, alpha_min, alpha_max
        )

        # 大M（保持你当前固定值）
        M_Mo = 8.9363
        M_Cu = 8.9363

        # 过滤后的索引集合
        I = range(num_of_SKUs)
        V = range(num_of_Vehicles)
        K = range(filtered_cnt)
        aK = range(filtered_cnt)

        cmdstring_output_V_IDs = None
        cmdstring_output_B_IDs = None

        model_data = ModelData(
            num_of_SKUs=num_of_SKUs,
            num_of_Vehicles=num_of_Vehicles,
            num_of_Batches=filtered_cnt,
            I=I,
            V=V,
            K=K,
            aK=aK,
            B_LB=B_LB,
            B_UB=B_UB,
            b_v=b_v,
            M_vk=M_vk,
            W_v_min=W_v_min,
            W_v_max=W_v_max,
            SKUs_Data=SKUs_Data,
            beta=beta,
            alpha=alpha,
            w=w,
            _w=_w,
            p=[],
            beta_min=beta_min,
            beta_max=beta_max,
            alpha_min=alpha_min,
            alpha_max=alpha_max,
            a=a,
            M_Mo=M_Mo,
            M_Cu=M_Cu,
            run_epagap=run_epagap,
            run_epgap=run_epgap,
            run_tilim=run_tilim,
            cmdstring_output_V_IDs=cmdstring_output_V_IDs,
            cmdstring_output_B_IDs=cmdstring_output_B_IDs,
            original_num_of_Batches=original_num_of_Batches,
            k_new_to_old=k_new_to_old
        )

        return model_data

    except Exception as e:
        print(f"[error]读取Excel文件时出错: {e}")
        import traceback
        traceback.print_exc()
        return None

def read_params_only(file_path: str) -> tuple[int, int]:
    """只读 Params 的 I/V，用于 precheck 模式，避免读取 inv_SKUs。"""
    num_of_SKUs = int(read_cell_value(file_path, "Params", "B1"))
    num_of_Vehicles = int(read_cell_value(file_path, "Params", "B2"))
    return num_of_SKUs, num_of_Vehicles