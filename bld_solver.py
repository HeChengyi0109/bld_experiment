"""
bld_benders_solver.py
Benders 求解模块：单阶段求解 + 结果写回 Excel
"""
from pyscipopt import SCIP_PARAMSETTING
import pandas as pd
import re
from pathlib import Path


from bld_data import ModelData, Solution
from bld_model import build_mip_model
# from bld_utils import write_results_to_excel
from bld_utils import write_df_to_sheet
from config import OUTPUT_FILE, ENABLE_PROGRESS_PLOTS
import os
import shutil
import time
import math


# 设置为 1800 表示强制运行 30 分钟；改成 2400 表示 40 分钟。
# 若想恢复为读取 input/data 中的 run_tilim，请改回 None。
FORCE_SCIP_TIME_LIMIT_SEC = 300.0


def _to_float_or_none(value):
    """尽量把字符串/数值转换成 float，失败时返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None

    s = str(value).strip()
    if s == "":
        return None

    low = s.lower()
    if low in {"--", "na", "n/a", "none", "unknown"}:
        return None
    if low in {"inf", "+inf", "infinity", "+infinity"}:
        return float("inf")
    if low in {"-inf", "-infinity"}:
        return float("-inf")

    if s.endswith('%'):
        s = s[:-1].strip()

    try:
        return float(s)
    except Exception:
        return None


def _fmt_metric(value, kind="number", digits=6):
    """统一日志打印格式。"""
    if value is None or pd.isna(value):
        return "N/A"

    try:
        value = float(value)
    except Exception:
        return str(value)

    if kind == "gap":
        if value == float("inf"):
            return "Inf"
        return f"{value:.{digits}f}%"

    if value == float("inf"):
        return "Inf"
    if value == float("-inf"):
        return "-Inf"
    return f"{value:.{digits}f}"


def _build_progress_paths(output_file: str) -> dict:
    """为 SCIP 过程日志、csv 和图片创建输出目录。"""
    output_path = Path(output_file).resolve()
    parent = output_path.parent
    stem = output_path.stem
    progress_dir = parent / f"{stem}_scip_progress"
    progress_dir.mkdir(parents=True, exist_ok=True)

    return {
        "dir": progress_dir,
        "log": progress_dir / "scip_raw.log",
        "csv": progress_dir / f"{stem}.csv",
        "gap_png": progress_dir / "gap_vs_time.png",
        "obj_png": progress_dir / "objective_vs_time.png",
        "delta_gap_png": progress_dir / "delta_gap_vs_time.png",
        "delta_obj_png": progress_dir / "delta_objective_vs_time.png",
        "gap_post_png": progress_dir / "gap_vs_time_post_feasible.png",
        "obj_post_png": progress_dir / "objective_vs_time_post_feasible.png",
        "delta_gap_post_png": progress_dir / "delta_gap_vs_time_post_feasible.png",
        "delta_obj_post_png": progress_dir / "delta_objective_vs_time_post_feasible.png",
    }


def _is_ascii_only_path(path_like) -> bool:
    """判断路径字符串是否为纯 ASCII，便于规避 Windows 下 SCIP 对中文路径写日志失败的问题。"""
    try:
        str(path_like).encode('ascii')
        return True
    except Exception:
        return False



def _get_ascii_safe_log_path(output_file: str, preferred_log_path: str | Path) -> Path:
    """
    为 SCIP 原生日志挑选一个更稳妥的写入路径。
    优先使用原目录；若路径含中文/非 ASCII，则回退到磁盘根目录下的 ASCII 安全目录。
    """
    preferred_log_path = Path(preferred_log_path)
    if _is_ascii_only_path(preferred_log_path):
        preferred_log_path.parent.mkdir(parents=True, exist_ok=True)
        return preferred_log_path

    output_path = Path(output_file).resolve()

    fallback_candidates = []

    drive = getattr(output_path, 'drive', '')
    if drive:
        fallback_candidates.append(Path(f"{drive}/bld_scip_progress_logs"))

    cwd_anchor = Path.cwd().anchor
    if cwd_anchor:
        fallback_candidates.append(Path(cwd_anchor) / 'bld_scip_progress_logs')

    fallback_candidates.extend([
        Path('C:/bld_scip_progress_logs'),
        Path('D:/bld_scip_progress_logs'),
        Path.cwd() / 'bld_scip_progress_logs_ascii',
    ])

    for base in fallback_candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            candidate = base / f'{output_path.stem}_scip_raw.log'
            if _is_ascii_only_path(candidate):
                return candidate
        except Exception:
            continue

    # 实在不行，仍返回原路径，后续由调用方兜底打印报错。
    preferred_log_path.parent.mkdir(parents=True, exist_ok=True)
    return preferred_log_path


def parse_scip_progress_log(log_path: str | Path) -> pd.DataFrame:
    """
    从 SCIP 原生日志中提取时间、dual bound、primal bound(当前最好目标值)、gap。
    仅解析 branch-and-bound 表格中的数据行，不改动原始日志。
    """
    log_path = Path(log_path)
    if not log_path.exists():
        return pd.DataFrame(columns=[
            "time_sec", "dual_bound", "objective_value", "gap_pct",
            "delta_gap", "delta_objective"
        ])

    line_pattern = re.compile(r'^\s*[A-Za-z*o]?\s*([0-9]+(?:\.[0-9]+)?)s\|')
    records = []

    with log_path.open('r', encoding='utf-8', errors='ignore') as f:
        for raw_line in f:
            line = raw_line.rstrip('\n')
            if not line_pattern.match(line):
                continue

            parts = [p.strip() for p in line.split('|')]
            if len(parts) < 18:
                continue

            m = line_pattern.match(line)
            if not m:
                continue

            time_sec = _to_float_or_none(m.group(1))
            dual_bound = _to_float_or_none(parts[-4])
            objective_value = _to_float_or_none(parts[-3])
            gap_pct = _to_float_or_none(parts[-2])

            records.append({
                "time_sec": time_sec,
                "dual_bound": dual_bound,
                "objective_value": objective_value,
                "gap_pct": gap_pct,
                "raw_line": line,
            })

    if not records:
        return pd.DataFrame(columns=[
            "time_sec", "dual_bound", "objective_value", "gap_pct",
            "delta_gap", "delta_objective"
        ])

    df = pd.DataFrame(records)

    # 用前值填充，体现“当前最好目标值 / 当前gap”的时间轨迹。
    if "objective_value" in df.columns:
        df["objective_value_ffill"] = df["objective_value"].ffill()
    else:
        df["objective_value_ffill"] = pd.Series(dtype=float)

    if "gap_pct" in df.columns:
        gap_for_delta = pd.to_numeric(df["gap_pct"], errors='coerce')
        gap_for_delta = gap_for_delta.replace([float('inf'), float('-inf')], float('nan'))
        df["gap_pct_ffill"] = gap_for_delta.ffill()
    else:
        df["gap_pct_ffill"] = pd.Series(dtype=float)

    df["objective_value_ffill"] = pd.to_numeric(df["objective_value_ffill"], errors='coerce')
    df["gap_pct_ffill"] = pd.to_numeric(df["gap_pct_ffill"], errors='coerce')

    df["delta_objective"] = df["objective_value_ffill"].diff()
    df["delta_gap"] = df["gap_pct_ffill"].diff()
    df["delta_objective"] = pd.to_numeric(df["delta_objective"], errors='coerce').fillna(0.0)
    df["delta_gap"] = pd.to_numeric(df["delta_gap"], errors='coerce').fillna(0.0)

    return df


def print_progress_summary(df: pd.DataFrame):
    """把解析后的时间序列重新打印成结构化日志。"""
    print("\n=== SCIP过程指标时间序列 ===")

    if df is None or df.empty:
        print("[progress]未从 SCIP 原生日志中解析到 branch-and-bound 进度行。")
        return

    for row in df.itertuples(index=False):
        print(
            "[progress] "
            f"t={_fmt_metric(getattr(row, 'time_sec', None), kind='number', digits=3)}s, "
            f"gap={_fmt_metric(getattr(row, 'gap_pct_ffill', None), kind='gap', digits=6)}, "
            f"objective={_fmt_metric(getattr(row, 'objective_value_ffill', None), kind='number', digits=6)}, "
            f"delta_gap={_fmt_metric(getattr(row, 'delta_gap', None), kind='gap', digits=6)}, "
            f"delta_objective={_fmt_metric(getattr(row, 'delta_objective', None), kind='number', digits=6)}"
        )


def save_progress_artifacts(df: pd.DataFrame, output_file: str, save_plots: bool = ENABLE_PROGRESS_PLOTS) -> dict:
    """保存过程数据 csv 和图片。横轴统一换算为分钟，并额外输出“首次可行解之后”的聚焦图。"""
    paths = _build_progress_paths(output_file)

    if df is None or df.empty:
        print("[progress]没有可保存的时间序列数据，跳过 csv/图片输出。")
        return {"dir": str(paths["dir"])}

    export_df = df.copy()
    export_df["time_sec"] = pd.to_numeric(export_df["time_sec"], errors='coerce')
    export_df["time_min"] = export_df["time_sec"] / 60.0
    export_df.to_csv(paths["csv"], index=False, encoding='utf-8-sig')

    if not save_plots:
        print("[progress]已按配置关闭绘图，仅输出 CSV。")
        return {
            "dir": str(paths["dir"]),
            "csv": str(paths["csv"]),
        }

    try:
        import matplotlib.pyplot as plt
    except Exception as e:
        print(f"[progress]导入 matplotlib 失败，无法绘图：{e}")
        return {
            "dir": str(paths["dir"]),
            "csv": str(paths["csv"]),
        }

    def _prepare_plot_data(x, y):
        x_series = pd.to_numeric(pd.Series(x), errors='coerce')
        y_series = pd.to_numeric(pd.Series(y), errors='coerce')
        valid_mask = (~x_series.isna()) & (~y_series.isna())

        if not valid_mask.any():
            return None, None

        x_plot = x_series[valid_mask].astype(float)
        y_plot = y_series[valid_mask].astype(float)
        return x_plot, y_plot

    def _get_xlim(df_plot: pd.DataFrame, full_range: bool):
        if df_plot is None or df_plot.empty:
            return None

        max_time_min = pd.to_numeric(df_plot["time_min"], errors='coerce').max()
        if pd.isna(max_time_min):
            return None

        x_max = max(1.0, math.ceil(float(max_time_min)))
        if full_range:
            return (0.0, x_max)

        min_time_min = pd.to_numeric(df_plot["time_min"], errors='coerce').min()
        if pd.isna(min_time_min):
            return (0.0, x_max)

        left = max(0.0, math.floor(float(min_time_min) * 10.0) / 10.0)
        right = max(left + 0.1, x_max)
        return (left, right)

    def _finalize_plot(ylabel: str, title: str, file_path: Path, xlim=None, add_zero_line: bool = False):
        plt.xlabel('Runtime (min)')
        plt.ylabel(ylabel)
        plt.title(title)
        if add_zero_line:
            plt.axhline(0.0, linewidth=1.0, alpha=0.5)
        if xlim is not None:
            plt.xlim(*xlim)
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(file_path, dpi=150)
        plt.close()

    def _save_step_plot(df_plot: pd.DataFrame, x_col: str, y_col: str, ylabel: str, title: str, file_path: Path, full_range: bool):
        if df_plot is None or df_plot.empty:
            print(f"[progress]图像 {title} 没有有效数据，跳过：{file_path}")
            return

        x_plot, y_plot = _prepare_plot_data(df_plot[x_col], df_plot[y_col])
        if x_plot is None:
            print(f"[progress]图像 {title} 没有有效数据，跳过：{file_path}")
            return

        plt.figure(figsize=(12, 4.8))
        plt.step(x_plot, y_plot, where='post')
        plt.scatter(x_plot, y_plot, s=18)
        _finalize_plot(ylabel, title, file_path, xlim=_get_xlim(df_plot, full_range=full_range))

    def _save_scatter_plot(df_plot: pd.DataFrame, x_col: str, y_col: str, ylabel: str, title: str, file_path: Path, full_range: bool):
        if df_plot is None or df_plot.empty:
            print(f"[progress]图像 {title} 没有有效数据，跳过：{file_path}")
            return

        x_plot, y_plot = _prepare_plot_data(df_plot[x_col], df_plot[y_col])
        if x_plot is None:
            print(f"[progress]图像 {title} 没有有效数据，跳过：{file_path}")
            return

        plt.figure(figsize=(12, 4.8))
        plt.scatter(x_plot, y_plot, s=22, alpha=0.85)
        _finalize_plot(ylabel, title, file_path, xlim=_get_xlim(df_plot, full_range=full_range), add_zero_line=True)

    main_plot_df = export_df[
        export_df["objective_value_ffill"].notna() | export_df["gap_pct_ffill"].notna()
    ].copy()
    post_feasible_df = export_df[export_df["objective_value_ffill"].notna()].copy()

    _save_step_plot(main_plot_df, "time_min", "gap_pct_ffill", 'Gap (%)', 'Gap vs Runtime (Full Range)', paths["gap_png"], full_range=True)
    _save_step_plot(main_plot_df, "time_min", "objective_value_ffill", 'Objective Value', 'Objective Value vs Runtime (Full Range)', paths["obj_png"], full_range=True)
    _save_scatter_plot(post_feasible_df, "time_min", "delta_gap", 'Delta Gap (%)', 'Delta Gap vs Runtime (Post-Feasible)', paths["delta_gap_png"], full_range=False)
    _save_scatter_plot(post_feasible_df, "time_min", "delta_objective", 'Delta Objective Value', 'Delta Objective Value vs Runtime (Post-Feasible)', paths["delta_obj_png"], full_range=False)

    _save_step_plot(post_feasible_df, "time_min", "gap_pct_ffill", 'Gap (%)', 'Gap vs Runtime (Post-Feasible)', paths["gap_post_png"], full_range=False)
    _save_step_plot(post_feasible_df, "time_min", "objective_value_ffill", 'Objective Value', 'Objective Value vs Runtime (Post-Feasible)', paths["obj_post_png"], full_range=False)
    _save_scatter_plot(post_feasible_df, "time_min", "delta_gap", 'Delta Gap (%)', 'Delta Gap vs Runtime (Post-Feasible Zoom)', paths["delta_gap_post_png"], full_range=False)
    _save_scatter_plot(post_feasible_df, "time_min", "delta_objective", 'Delta Objective Value', 'Delta Objective Value vs Runtime (Post-Feasible Zoom)', paths["delta_obj_post_png"], full_range=False)

    return {
        "dir": str(paths["dir"]),
        "csv": str(paths["csv"]),
        "gap_png": str(paths["gap_png"]),
        "obj_png": str(paths["obj_png"]),
        "delta_gap_png": str(paths["delta_gap_png"]),
        "delta_obj_png": str(paths["delta_obj_png"]),
        "gap_post_png": str(paths["gap_post_png"]),
        "obj_post_png": str(paths["obj_post_png"]),
        "delta_gap_post_png": str(paths["delta_gap_post_png"]),
        "delta_obj_post_png": str(paths["delta_obj_post_png"]),
    }


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
                # 注意：用 best_sol 取值，而不是 model.getVal()
                if model.getSolVal(best_sol, x[i, v, k]) > 0.5:
                    V_IDs[i] = v + 1
                    B_IDs[i] = k + 1
                    assigned = True
                    break
            if assigned:
                break

    # 目标值也用 best_sol 更稳妥
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
    保存求解结果到固定 output.xlsx（output2.0 格式）
    - 若 output.xlsx 不存在：用 output2.0.xlsx 初始化一次
    - 每次运行覆盖写 SKUs 与 VBResult
    """

    # output_file = OUTPUT_FILE

    # -----------------
    # 1) SKUs sheet
    # -----------------
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

    # -----------------
    # 2) VBResult sheet
    # -----------------
    rows = []
    V = data.num_of_Vehicles
    K = data.num_of_Batches

    for v in range(1, V + 1):      # 1-based
        for k in range(1, K + 1):  # 1-based
            idx = [i for i in range(data.num_of_SKUs) if solution.V_IDs[i] == v and solution.B_IDs[i] == k]

            W = sum(data.w[i] for i in idx) if idx else 0.0
            Wdry = sum(data._w[i] for i in idx) if idx else 0.0

            mo_metal = 100*sum(data.beta[i] * data._w[i] for i in idx) if idx else 0.0
            cu_metal = 100*sum(data.alpha[i] * data._w[i] for i in idx) if idx else 0.0

            mo_grade = (mo_metal / Wdry) if Wdry > 0 else 0.0
            cu_grade = (cu_metal / Wdry) if Wdry > 0 else 0.0
            moisture = ((W - Wdry) / W) if W > 0 else 0.0

            unit_price = float(data.a[k - 1])  # 0-based list
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
'''
def print_solution_summary(solution: Solution, data: ModelData):
    """
    打印求解摘要（使用解中的 V/B 计算批次数）
    """
    print("\n=== 求解结果摘要 ===")
    print(f"求解状态: {solution.status}")

    if solution.status == "optimal":
        print(f"找到最优解，目标值: {solution.objective_value:.2f}")
    elif solution.status == "timelimit":
        print(f"达到时间限制，目标值: {solution.objective_value:.2f}")
        print(f"当前间隙: {solution.gap}")
    elif solution.status == "gaplimit":
        print(f"达到间隙限制，目标值: {solution.objective_value:.2f}")
        print(f"当前间隙: {solution.gap}")

    vk_pairs = set()
    for i in range(data.num_of_SKUs):
        v = solution.V_IDs[i]
        b = solution.B_IDs[i]
        if v > 0 and b > 0:
            vk_pairs.add((v, b))

    print(f"使用的批次数量: {len(vk_pairs)}")
    print(f"使用的车辆数量: {len({v for v in solution.V_IDs if v > 0})}")
    print(f"分配的 SKU 数量: {sum(1 for v in solution.V_IDs if v > 0)}")
'''
def print_solution_summary(
    solution: Solution,
    data: ModelData,
    runtime_sec: float | None = None,
    saved_file: str | None = None,
):
    """
    打印求解摘要（按用户指定顺序，每项一行；不显示原批次编号）
    """

    def _fmt(x: float) -> str:
        # 兼顾 0.5 / 0.005 这类数值的可读性：6 位有效数字
        try:
            return f"{float(x):.6g}"
        except Exception:
            return str(x)

    print("\n=== 求解结果摘要 ===")
    # 1) 运行时间
    if runtime_sec is None:
        runtime_str = "N/A"
    else:
        runtime_str = f"{runtime_sec:.3f} s"
    print(f"[result]运行时间：{runtime_str}")

    # 2) 求解状态
    print(f"[result]求解状态：{solution.status}")

    # 3) 目标值
    obj = solution.objective_value
    obj_str = "N/A" if obj is None else f"{obj:.6f}"
    print(f"[result]目标值：{obj_str}")

    # 基于解提取使用情况
    used_vehicles = {v for v in solution.V_IDs if v and v > 0}
    assigned_skus = sum(1 for v in solution.V_IDs if v and v > 0)
    used_batches = {b for b in solution.B_IDs if b and b > 0}  # B_ID 为 1-based

    # 4) 使用批次数量（按“批次ID去重”）
    print(f"[result]使用批次数量：{len(used_batches)}")

    # 5/6) 使用批次钼/铜品位区间（去重后展示）
    mo_intervals = set()
    cu_intervals = set()
    for b in sorted(used_batches):
        idx = b - 1  # 1-based -> 0-based
        if 0 <= idx < data.num_of_Batches:
            mo_intervals.add((data.beta_min[idx], data.beta_max[idx]))
            cu_intervals.add((data.alpha_min[idx], data.alpha_max[idx]))

    if mo_intervals:
        mo_str = "; ".join(f"{_fmt(lo)}–{_fmt(hi)}" for lo, hi in sorted(mo_intervals))
    else:
        mo_str = "N/A"
    print(f"[result]使用批次钼品位区间：{mo_str}")

    if cu_intervals:
        cu_str = "; ".join(f"{_fmt(lo)}–{_fmt(hi)}" for lo, hi in sorted(cu_intervals))
    else:
        cu_str = "N/A"
    print(f"[result]使用批次铜品位区间：{cu_str}")

    # 7) 使用车次数量
    print(f"[result]使用车次数量：{len(used_vehicles)}")

    # 8) 使用SKU数量
    print(f"[result]使用SKU数量：{assigned_skus}")

    '''
    # 9) success 行
    if saved_file:
        print(f"[success]求解完成！结果已保存到：{saved_file}")
    else:
        print("[success]求解完成！结果已保存到：N/A")
    '''

# ==========================
def solve_benders_model(data: ModelData, input_file: str, output_file: str):
    """
    兼容原 main.py 的调用名：仍叫 solve_benders_model，
    但内部改为“直接用 SCIP 求解单体 MIP”。
    达到 data.run_tilim 时自动停止，并输出当前最好解（bestSol）。
    """

    print("[debug] loaded solver file:", __file__)
    print("构建单体 MIP 模型（不使用 Benders）...")
    model = build_mip_model(data)
    x = model.data["x"]
    delta = model.data["delta"]

    print("配置 SCIP 求解参数 ...")

    print(f"[debug] run_epgap = {getattr(data, 'run_epgap', None)}")
    print(f"[debug] run_epagap = {getattr(data, 'run_epagap', None)}")
    print(f"[debug] run_tilim = {getattr(data, 'run_tilim', None)}")
    print(f"[debug] FORCE_SCIP_TIME_LIMIT_SEC = {FORCE_SCIP_TIME_LIMIT_SEC}")

    try:
        model.setPresolve(SCIP_PARAMSETTING.OFF)
        model.setPropagating(SCIP_PARAMSETTING.DEFAULT)
        model.setHeuristics(SCIP_PARAMSETTING.DEFAULT)
    except Exception:
        pass


    try:
        model.setPresolve(SCIP_PARAMSETTING.OFF)
        model.setPropagating(SCIP_PARAMSETTING.DEFAULT)
        model.setHeuristics(SCIP_PARAMSETTING.DEFAULT)
    except Exception:
        pass

    # if getattr(data, "run_epgap", 0) and float(data.run_epgap) > 0:
    #     model.setParam("limits/gap", float(data.run_epgap))
    # if getattr(data, "run_epagap", 0) and float(data.run_epagap) > 0:
    #     model.setParam("limits/absgap", float(data.run_epagap))

    configured_time_limit = getattr(data, "run_tilim", 0)
    effective_time_limit = FORCE_SCIP_TIME_LIMIT_SEC if FORCE_SCIP_TIME_LIMIT_SEC is not None else configured_time_limit
    if effective_time_limit and float(effective_time_limit) > 0:
        model.setParam("limits/time", float(effective_time_limit))
        print(f"[progress]当前 SCIP 时间限制：{float(effective_time_limit):.1f} 秒（{float(effective_time_limit) / 60.0:.2f} 分钟）")

    model.setParam("display/verblevel", 4)

    progress_paths = _build_progress_paths(output_file)
    raw_log_target = progress_paths["log"]
    log_parse_source = raw_log_target
    try:
        scip_log_path = _get_ascii_safe_log_path(output_file, raw_log_target)

        cleanup_paths = []
        for candidate in [raw_log_target, scip_log_path]:
            candidate = Path(candidate)
            if candidate not in cleanup_paths:
                cleanup_paths.append(candidate)

        for old_log_path in cleanup_paths:
            try:
                if old_log_path.exists():
                    old_log_path.unlink()
                    print(f"[progress]已删除旧的 SCIP 原生日志：{old_log_path}")
            except Exception as cleanup_error:
                print(f"[progress]删除旧日志失败，但将继续本次运行：{old_log_path}，原因：{cleanup_error}")

        model.setLogfile(str(scip_log_path))
        log_parse_source = scip_log_path
        if Path(scip_log_path).resolve() == Path(raw_log_target).resolve():
            print(f"[progress]SCIP原生日志将额外保存到：{raw_log_target}")
        else:
            print(f"[progress]项目目录包含非 ASCII 路径，SCIP 原生日志改为写入：{scip_log_path}")
            print(f"[progress]后续解析结果（CSV/图片）仍输出到：{progress_paths['dir']}")
    except Exception as e:
        log_parse_source = None
        print(f"[progress]设置 SCIP 日志文件失败，将仅保留控制台日志：{e}")

    print("开始求解 MIP ...")

    t0 = time.time()
    model.optimize()
    runtime_sec = time.time() - t0

    try:
        model.setLogfile(None)
    except Exception:
        pass

    progress_df = parse_scip_progress_log(log_parse_source) if log_parse_source else pd.DataFrame()
    print_progress_summary(progress_df)
    progress_artifacts = save_progress_artifacts(progress_df, output_file)
    if progress_artifacts:
        print(f"[progress]过程数据与图片输出目录：{progress_artifacts.get('dir', 'N/A')}")
        if progress_artifacts.get('csv'):
            print(f"[progress]过程数据 CSV：{progress_artifacts['csv']}")
        if progress_artifacts.get('gap_png'):
            print(f"[progress]图1 Gap 全程曲线（分钟制）：{progress_artifacts['gap_png']}")
        if progress_artifacts.get('obj_png'):
            print(f"[progress]图2 目标值全程曲线（分钟制）：{progress_artifacts['obj_png']}")
        if progress_artifacts.get('delta_gap_png'):
            print(f"[progress]图3 Delta Gap 散点图（首次可行解后）：{progress_artifacts['delta_gap_png']}")
        if progress_artifacts.get('delta_obj_png'):
            print(f"[progress]图4 Delta 目标值散点图（首次可行解后）：{progress_artifacts['delta_obj_png']}")
        if progress_artifacts.get('gap_post_png'):
            print(f"[progress]图5 Gap 聚焦曲线（首次可行解后）：{progress_artifacts['gap_post_png']}")
        if progress_artifacts.get('obj_post_png'):
            print(f"[progress]图6 目标值聚焦曲线（首次可行解后）：{progress_artifacts['obj_post_png']}")
        if progress_artifacts.get('delta_gap_post_png'):
            print(f"[progress]图7 Delta Gap 聚焦散点图：{progress_artifacts['delta_gap_post_png']}")
        if progress_artifacts.get('delta_obj_post_png'):
            print(f"[progress]图8 Delta 目标值聚焦散点图：{progress_artifacts['delta_obj_post_png']}")

    status = str(model.getStatus())
    best_sol = model.getBestSol()

    try:
        n_sols = int(model.getNSols())
    except Exception:
        try:
            n_sols = len(model.getSols())
        except Exception:
            n_sols = 0

    print(f"[SCIP] Solver status: {status}")
    print(f"[debug] n_sols = {n_sols}")
    print(f"[debug] best_sol_is_none = {best_sol is None}")
    print(f"[debug] best_sol_type = {type(best_sol)}")

    if n_sols == 0 or best_sol is None:
        print("[debug] entering no-solution branch")
        solution = Solution(
            status=status,
            objective_value=None,
            V_IDs=[0] * data.num_of_SKUs,
            B_IDs=[0] * data.num_of_SKUs,
            gap=None,
        )
        solution.has_solution = False
        print("[error]在时间限制内未找到任何可行解。不生成输出文件。")
        return solution, None, model, None

    print("[debug] entering save branch")
    # 调试用，不能加，加了会强制报错
    # raise RuntimeError("SAVE_BRANCH_REACHED")

    # 有可行解
    try:
        best_obj = float(model.getSolObjVal(best_sol))
    except Exception:
        best_obj = float(model.getObjVal())

    try:
        gap = float(model.getGap())
    except Exception:
        gap = None

    print(f"[SCIP] Best objective (bestSol): {best_obj}")
    if gap is not None:
        print(f"[SCIP] Final relative gap: {gap}")

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

    solution = Solution(
        status=status,
        objective_value=best_obj,
        V_IDs=V_IDs,
        B_IDs=B_IDs,
        gap=gap,
    )
    solution.has_solution = True

    saved_file = save_solution(solution, data, input_file, output_file)
    print_solution_summary(solution, data, runtime_sec=runtime_sec, saved_file=saved_file)

    return solution, saved_file, model, None

