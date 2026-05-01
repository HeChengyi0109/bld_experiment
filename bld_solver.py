"""
bld_solver.py
整体 MIP 求解器：
1. 先用“按车辆顺序逐车求解”的策略构造一个完整可行解；
2. 再把这个可行解作为 SCIP 的初始解（MIP start / primal solution）；
3. 最后继续求解原始整体 MIP。

当前版本额外支持：
- warmstart 阶段仍按原逻辑使用 data.run_tilim；
- warmstart 结束后的“整车整体 MIP”阶段，单独强制运行 40 分钟；
- 仅对“整车整体 MIP”阶段记录 SCIP 原生日志、CSV 和 8 张过程图。
"""
from dataclasses import replace
import os
import math
import re
import shutil
import time
from pathlib import Path

import pandas as pd
from pyscipopt import SCIP_PARAMSETTING

from bld_data import ModelData, Solution, preprocess_sku_data
from bld_model import build_mip_model
from bld_utils import write_df_to_sheet


# 仅作用于 warmstart 初始解注入 SCIP 之后的“整车整体 MIP”主求解阶段。
# 2400 秒 = 40 分钟。
# 如果想恢复为读取 Excel Params!B12，请改成 None，并且运行 bld_main.py 时不要传 --main-time-limit。
FORCE_MAIN_MIP_TIME_LIMIT_SEC = None

# 主 MIP 阶段是否使用 Excel 中的 gap/absolute gap 提前停机。
# 做固定时间实验时建议 False；生产求解想达到 gap 后提前停，可改 True。
APPLY_MAIN_MIP_GAP_LIMITS = False


def _resolve_main_mip_time_limit(data: ModelData, main_time_limit_sec: float | None = None):
    """
    决定 warmstart 注入后，整体 MIP 主求解阶段的 SCIP 时间上限。
    优先级：
    1) 命令行 --main-time-limit；
    2) 本文件常量 FORCE_MAIN_MIP_TIME_LIMIT_SEC；
    3) Excel Params!B12，即 data.run_tilim。
    """
    if main_time_limit_sec is not None:
        value = float(main_time_limit_sec)
        return (value if value > 0 else None), "--main-time-limit"

    if FORCE_MAIN_MIP_TIME_LIMIT_SEC is not None:
        value = float(FORCE_MAIN_MIP_TIME_LIMIT_SEC)
        return (value if value > 0 else None), "FORCE_MAIN_MIP_TIME_LIMIT_SEC"

    value = float(getattr(data, "run_tilim", 0) or 0)
    return (value if value > 0 else None), "Excel Params!B12 / data.run_tilim"


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
    """为整车整体 MIP 的 SCIP 过程日志、csv 和图片创建输出目录。"""
    output_path = Path(output_file).resolve()
    parent = output_path.parent
    stem = output_path.stem
    progress_dir = parent / f"{stem}_scip_progress"
    progress_dir.mkdir(parents=True, exist_ok=True)

    return {
        "dir": progress_dir,
        "log": progress_dir / "scip_raw.log",
        "csv": progress_dir / "scip_progress_data.csv",
        "gap_png": progress_dir / "gap_vs_time.png",
        "obj_png": progress_dir / "objective_vs_time.png",
        "delta_gap_png": progress_dir / "delta_gap_vs_time.png",
        "delta_obj_png": progress_dir / "delta_objective_vs_time.png",
        "gap_post_png": progress_dir / "gap_vs_time_post_feasible.png",
        "obj_post_png": progress_dir / "objective_vs_time_post_feasible.png",
        "delta_gap_post_png": progress_dir / "delta_gap_vs_time_post_feasible.png",
        "delta_obj_post_png": progress_dir / "delta_objective_vs_time_post_feasible.png",
    }


def _cleanup_old_progress_artifacts(output_file: str) -> dict:
    """删除当前 case 旧的过程产物，避免旧 csv/png 在本次运行失败或未解析到数据时被误复用。"""
    paths = _build_progress_paths(output_file)
    for key, path in paths.items():
        if key == "dir":
            continue
        try:
            path = Path(path)
            if path.exists():
                path.unlink()
                print(f"[progress]已删除旧过程文件：{path}")
        except Exception as cleanup_error:
            print(f"[progress]删除旧过程文件失败，但将继续本次运行：{path}，原因：{cleanup_error}")
    return paths


def _is_ascii_only_path(path_like) -> bool:
    """判断路径字符串是否为纯 ASCII，规避 Windows 下 SCIP 对中文路径写日志失败的问题。"""
    try:
        str(path_like).encode('ascii')
        return True
    except Exception:
        return False



def _get_ascii_safe_log_path(output_file: str, preferred_log_path: str | Path) -> Path:
    """
    为 SCIP 原生日志挑选更稳妥的写入路径。
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

    safe_stem = ''.join(ch if ch.isalnum() or ch in {'-', '_'} else '_' for ch in output_path.stem) or 'case'
    unique_name = f"{safe_stem}_pid{os.getpid()}_scip_raw.log"

    for base in fallback_candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            candidate = base / unique_name
            if _is_ascii_only_path(candidate):
                return candidate
        except Exception:
            continue

    preferred_log_path.parent.mkdir(parents=True, exist_ok=True)
    return preferred_log_path





def _persist_main_mip_log(log_source: str | Path | None, log_target: str | Path) -> Path | None:
    """
    把“整车整体 MIP 阶段”的 SCIP 原生日志，最终落回到项目输出目录中。
    - 若 log_source 与 log_target 相同，则直接返回目标路径；
    - 若 SCIP 因非 ASCII 路径限制写到了外部路径，则在求解结束后复制回输出目录；
    - 后续解析 csv / 作图统一读取这份落回输出目录的日志。
    """
    if not log_source:
        return None

    source = Path(log_source)
    target = Path(log_target)

    if not source.exists():
        print(f"[progress]整车整体 MIP 原生日志不存在，无法持久化：{source}")
        return None

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"[progress]创建日志输出目录失败：{target.parent}，原因：{e}")
        return None

    try:
        if source.resolve() == target.resolve():
            return target
    except Exception:
        if str(source) == str(target):
            return target

    try:
        shutil.copyfile(source, target)
        print(f"[progress]已将整车整体 MIP 原生日志复制回输出目录：{target}")
        return target
    except Exception as e:
        print(f"[progress]复制整车整体 MIP 原生日志失败：{source} -> {target}，原因：{e}")
        return source


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
    print("\n=== 整车整体 MIP 过程指标时间序列 ===")

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



def save_progress_artifacts(df: pd.DataFrame, output_file: str) -> dict:
    """保存过程数据 csv 和图片。横轴统一换算为分钟，并额外输出“首次可行解之后”的聚焦图。"""
    paths = _build_progress_paths(output_file)

    if df is None or df.empty:
        print("[progress]没有可保存的时间序列数据，跳过 csv/图片输出。")
        return {"dir": str(paths["dir"])}

    export_df = df.copy()
    export_df["time_sec"] = pd.to_numeric(export_df["time_sec"], errors='coerce')
    export_df["time_min"] = export_df["time_sec"] / 60.0
    export_df.to_csv(paths["csv"], index=False, encoding='utf-8-sig')

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

    try:
        gap = float(model.getGap())
    except Exception:
        gap = 0.0

    return Solution(
        status=status,
        objective_value=objective_value,
        V_IDs=V_IDs,
        B_IDs=B_IDs,
        gap=gap,
    )



def save_solution(solution: Solution, data: ModelData, input_file: str, output_file: str) -> str:
    """保存求解结果到 output.xlsx"""
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
    """打印求解摘要"""

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

    if saved_file:
        print(f"[result]结果文件：{saved_file}")



def _configure_model_params(
    model,
    data: ModelData,
    time_limit_override: float | None = None,
    force_time_limit: bool = False,
    apply_gap_limits: bool = True,
):
    """统一配置 SCIP 参数。"""
    try:
        model.setPresolve(SCIP_PARAMSETTING.OFF)
        model.setPropagating(SCIP_PARAMSETTING.DEFAULT)
        model.setHeuristics(SCIP_PARAMSETTING.DEFAULT)
    except Exception:
        pass

    if apply_gap_limits:
        if getattr(data, "run_epgap", 0) and float(data.run_epgap) > 0:
            model.setParam("limits/gap", float(data.run_epgap))
        if getattr(data, "run_epagap", 0) and float(data.run_epagap) > 0:
            model.setParam("limits/absgap", float(data.run_epagap))
    else:
        print("[progress]整车整体 MIP 阶段已禁用 run_epgap / run_epagap 提前停机，仅保留时间上限。")

    time_limit = time_limit_override
    if time_limit is None and not force_time_limit:
        time_limit = float(getattr(data, "run_tilim", 0) or 0)

    if time_limit and float(time_limit) > 0:
        model.setParam("limits/time", float(time_limit))

    model.setParam("display/verblevel", 4)



def _solve_single_vehicle_mip(data: ModelData):
    """对单车子问题求解一次 MIP；这里假定 data.num_of_Vehicles == 1。"""
    model = build_mip_model(data)
    x = model.data["x"]

    print("配置 SCIP 求解参数（单车热启动子问题）...")
    _configure_model_params(model, data)

    t0 = time.time()
    model.optimize()
    runtime_sec = time.time() - t0

    status = str(model.getStatus())
    best_sol = model.getBestSol()
    print(f"[SCIP-SEQ] Solver status: {status}")

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
    print(f"[SCIP-SEQ] Best objective (bestSol): {solution.objective_value}")
    if solution.gap > 0:
        print(f"[SCIP-SEQ] Final relative gap: {solution.gap}")

    return solution, model, runtime_sec



def _build_single_vehicle_data(base_data: ModelData, remaining_global_indices: list[int], remaining_time: float) -> ModelData:
    """从原始数据中抽取“剩余 SKU”，构造单车子问题数据。"""
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



def _build_sequential_warmstart_solution(data: ModelData):
    """按车辆逐车求解，构造一个可注入整体模型的完整可行解。"""
    total_vehicle_num = int(data.num_of_Vehicles)
    original_num_skus = int(data.num_of_SKUs)
    total_time_limit = float(getattr(data, "run_tilim", 0) or 0)

    global_V_IDs = [0] * original_num_skus
    global_B_IDs = [0] * original_num_skus
    remaining_global_indices = list(range(original_num_skus))

    total_obj = 0.0
    total_runtime = 0.0
    last_model = None

    print("构建顺序配矿热启动解（按车辆逐车求解）...")
    if total_time_limit > 0:
        print(f"[warmstart] 顺序热启动阶段沿用原始 run_tilim={total_time_limit:.3f}s")
    else:
        print("[warmstart] 顺序热启动阶段未设置显式 run_tilim，沿用原始无限制逻辑。")

    for vehicle_id in range(1, total_vehicle_num + 1):
        print(f"\n================ 当前求解车辆 {vehicle_id}/{total_vehicle_num} ================")

        if len(remaining_global_indices) == 0:
            print(f"[warmstart] 在求解第 {vehicle_id} 车前，剩余 SKU 已为空，无法继续构造完整热启动解。")
            return None, total_runtime, last_model

        remaining_wet_weight = sum(float(data.w[i]) for i in remaining_global_indices)
        if remaining_wet_weight + 1e-9 < float(data.W_v_min):
            print(
                f"[warmstart] 在求解第 {vehicle_id} 车前，剩余 SKU 总湿重={remaining_wet_weight:.6f}，"
                f"已小于单车最低装载量 W_v_min={float(data.W_v_min):.6f}，无法继续构造完整热启动解。"
            )
            return None, total_runtime, last_model

        if total_time_limit > 0:
            remaining_time = total_time_limit - total_runtime
            if remaining_time <= 1e-9:
                print(f"[warmstart] 顺序热启动阶段已耗尽其原始 run_tilim，停止于第 {vehicle_id} 车之前。")
                return None, total_runtime, last_model
        else:
            remaining_time = 0.0

        vehicle_data = _build_single_vehicle_data(data, remaining_global_indices, remaining_time)
        vehicle_solution, vehicle_model, vehicle_runtime = _solve_single_vehicle_mip(vehicle_data)
        total_runtime += vehicle_runtime
        last_model = vehicle_model

        if vehicle_solution.status in {"infeasible", "inforunbd", "unbounded"}:
            print(f"[warmstart] 第 {vehicle_id} 车单车模型不可行，热启动构造终止。")
            return None, total_runtime, last_model

        local_selected = []
        for local_i in range(vehicle_data.num_of_SKUs):
            if vehicle_solution.V_IDs[local_i] > 0:
                global_i = remaining_global_indices[local_i]
                global_V_IDs[global_i] = vehicle_id
                global_B_IDs[global_i] = vehicle_solution.B_IDs[local_i]
                local_selected.append(local_i)

        if not local_selected:
            print(f"[warmstart] 第 {vehicle_id} 车没有选中任何 SKU，热启动构造终止。")
            return None, total_runtime, last_model

        selected_global = {remaining_global_indices[local_i] for local_i in local_selected}
        remaining_global_indices = [idx for idx in remaining_global_indices if idx not in selected_global]
        total_obj += float(vehicle_solution.objective_value)

        loaded_weight = sum(float(data.w[i]) for i in selected_global)
        print(
            f"[warmstart] 第 {vehicle_id} 车完成：已分配 SKU 数={len(selected_global)}，"
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
    final_solution.has_solution = True
    return final_solution, total_runtime, last_model



def _inject_warmstart_solution(model, data: ModelData, heuristic_solution: Solution) -> bool:
    """把顺序配矿结果转换为整体模型的原始空间 primal solution，并添加到 SCIP。"""
    if heuristic_solution is None:
        return False

    x = model.data["x"]
    delta = model.data["delta"]

    create_orig_sol = getattr(model, "createOrigSol", None)
    if create_orig_sol is not None:
        sol = create_orig_sol()
    else:
        sol = model.createSol()

    for (_, _, _), var in x.items():
        model.setSolVal(sol, var, 0.0)
    for (_, _), var in delta.items():
        model.setSolVal(sol, var, 0.0)

    used_pairs = set()
    assigned_cnt = 0
    for i in range(data.num_of_SKUs):
        v_id = int(heuristic_solution.V_IDs[i])
        b_id = int(heuristic_solution.B_IDs[i])
        if v_id <= 0 or b_id <= 0:
            continue

        v = v_id - 1
        k = b_id - 1
        if (i, v, k) not in x:
            raise KeyError(f"warm-start 映射失败：找不到变量 x[{i},{v},{k}]。")

        model.setSolVal(sol, x[i, v, k], 1.0)
        used_pairs.add((v, k))
        assigned_cnt += 1

    for v, k in used_pairs:
        model.setSolVal(sol, delta[v, k], 1.0)

    stored = model.addSol(sol, free=True)
    print(
        f"[warmstart] 已向 SCIP 注入初始解：assigned_skus={assigned_cnt}, "
        f"used_vk_pairs={len(used_pairs)}, stored={stored}"
    )
    return bool(stored)



def solve_benders_model(
    data: ModelData,
    input_file: str,
    output_file: str,
    use_warmstart: bool = True,
    main_time_limit_sec: float | None = None,
):
    """
    保留原函数名，但内部逻辑变为：
    - 先构造顺序配矿热启动解；
    - 再求解原始整体 MIP；
    - 若热启动不可用，则自动回退为冷启动。

    计时逻辑：
    - warmstart 阶段：保持原逻辑，沿用 data.run_tilim；
    - 整车整体 MIP 阶段：固定强制运行 40 分钟（2400 秒），不再扣减 warmstart 用时。
    """
    print("[debug] loaded solver file:", __file__)
    print("构建整体 MIP 模型（使用顺序配矿结果作为热启动）...")

    print(f"[debug] run_epgap = {getattr(data, 'run_epgap', None)}")
    print(f"[debug] run_epagap = {getattr(data, 'run_epagap', None)}")
    print(f"[debug] run_tilim = {getattr(data, 'run_tilim', None)}")
    print(f"[debug] FORCE_MAIN_MIP_TIME_LIMIT_SEC = {FORCE_MAIN_MIP_TIME_LIMIT_SEC}")
    print(f"[debug] APPLY_MAIN_MIP_GAP_LIMITS = {APPLY_MAIN_MIP_GAP_LIMITS}")

    effective_main_time_limit, main_time_limit_source = _resolve_main_mip_time_limit(data, main_time_limit_sec)
    print(f"[debug] main_time_limit_source = {main_time_limit_source}")
    print(f"[debug] effective_main_mip_time_limit_sec = {effective_main_time_limit}")

    heuristic_solution = None
    heuristic_runtime = 0.0
    if use_warmstart:
        heuristic_solution, heuristic_runtime, _ = _build_sequential_warmstart_solution(data)
        if heuristic_solution is None:
            print("[warmstart] 未能构造完整可行初始解，将自动回退为冷启动。")
        else:
            assigned_skus = sum(1 for v in heuristic_solution.V_IDs if v > 0)
            used_batches = len({b for b in heuristic_solution.B_IDs if b > 0})
            print(
                f"[warmstart] 顺序配矿热启动解已生成：目标值={heuristic_solution.objective_value:.6f}, "
                f"已分配 SKU={assigned_skus}, 使用批次={used_batches}, 用时={heuristic_runtime:.3f}s"
            )
    else:
        print("[warmstart] 已按参数要求禁用 warmstart，将直接冷启动整车整体 MIP。")

    model = build_mip_model(data)
    x = model.data["x"]

    print("配置 SCIP 求解参数（整车整体 MIP）...")
    _configure_model_params(
        model,
        data,
        time_limit_override=effective_main_time_limit,
        force_time_limit=True,
        apply_gap_limits=APPLY_MAIN_MIP_GAP_LIMITS,
    )
    if effective_main_time_limit is not None:
        print(
            f"[progress]整车整体 MIP 阶段强制时间限制：{float(effective_main_time_limit):.1f} 秒"
            f"（{float(effective_main_time_limit) / 60.0:.2f} 分钟），来源：{main_time_limit_source}"
        )
    else:
        print("[progress]整车整体 MIP 阶段未设置显式时间限制。")
    if heuristic_runtime > 0:
        print(
            f"[progress]warmstart 阶段实际耗时：{heuristic_runtime:.3f} 秒；"
            "该耗时不会扣减整车整体 MIP 主求解阶段的时间上限。"
        )

    warmstart_loaded = False
    if heuristic_solution is not None:
        warmstart_loaded = _inject_warmstart_solution(model, data, heuristic_solution)
        if not warmstart_loaded:
            print("[warmstart] SCIP 未接受初始解，将继续冷启动求解整体模型。")

    progress_paths = _cleanup_old_progress_artifacts(output_file)
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
            print(f"[progress]SCIP 原生日志将保存到：{raw_log_target}")
        else:
            print(f"[progress]项目目录包含非 ASCII 路径，SCIP 原生日志改为写入：{scip_log_path}")
            print(f"[progress]后续解析结果（CSV/图片）仍输出到：{progress_paths['dir']}")
    except Exception as e:
        log_parse_source = None
        print(f"[progress]设置 SCIP 日志文件失败，将仅保留控制台日志：{e}")

    print("开始求解整体 MIP ...")
    t0 = time.time()
    model.optimize()
    main_runtime = time.time() - t0
    total_runtime = heuristic_runtime + main_runtime

    try:
        model.setLogfile(None)
    except Exception:
        pass

    final_main_mip_log = _persist_main_mip_log(log_parse_source, raw_log_target) if log_parse_source else None
    progress_df = parse_scip_progress_log(final_main_mip_log) if final_main_mip_log else pd.DataFrame()
    print_progress_summary(progress_df)
    progress_artifacts = save_progress_artifacts(progress_df, output_file)
    if progress_artifacts:
        print(f"[progress]过程数据与图片输出目录：{progress_artifacts.get('dir', 'N/A')}")
        if final_main_mip_log:
            print(f"[progress]整车整体 MIP 原生日志：{final_main_mip_log}")
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
    print(f"[debug] warmstart_loaded = {warmstart_loaded}")
    print(f"[debug] main_runtime_sec = {main_runtime:.3f}")
    print(f"[debug] total_runtime_sec = {total_runtime:.3f}")

    if n_sols == 0 or best_sol is None:
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

    solution = extract_solution(model, x, data, best_sol)
    solution.has_solution = True

    print(f"[SCIP] Best objective (bestSol): {solution.objective_value}")
    if solution.gap is not None:
        print(f"[SCIP] Final relative gap: {solution.gap}")

    saved_file = save_solution(solution, data, input_file, output_file)
    print_solution_summary(solution, data, runtime_sec=total_runtime, saved_file=saved_file)

    return solution, saved_file, model, None
