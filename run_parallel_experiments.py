"""
run_parallel_experiments.py

批量运行 greedy-warmstart + SCIP 场景。

含义：
- i -> Params!B1 -> num_of_SKUs
- v -> Params!B2 -> num_of_Vehicles
- t -> 文件名标签，同时通过 --main-time-limit 控制 warm-start 注入后的 SCIP 主求解时间

每个场景等价于执行一次：
python bld_main.py --mode solve --method scip --input <case_input.xlsx> --output <case_output.xlsx> --main-time-limit <TIME_LIMIT_SEC>
"""

from __future__ import annotations

import csv
import shutil
import subprocess
import sys
import time
from pathlib import Path

from openpyxl import load_workbook

# ==============================
# 基础路径
# ==============================
BASE_DIR = Path(__file__).resolve().parent
BASE_INPUT = BASE_DIR / "input.xlsx"
MAIN_SCRIPT = BASE_DIR / "bld_main.py"
PYTHON_EXE = sys.executable

# ==============================
# 批跑配置
# ==============================
SCENARIOS = [
    # (200, 2),
    # (300, 2),
    # (200, 3),
    # (300, 3),
    (200, 4),
    (200, 5),
    (300, 10),
]

# 这是 greedy 初始解注入 SCIP 之后，model.optimize() 的时间上限。
# 注意：greedy 构造初始解的耗时不计入这个时间。
TIME_LIMIT_SEC = 300

# 并发数建议不要太高；SCIP 很吃 CPU/内存。
MAX_CONCURRENT = min(4, len(SCENARIOS))
POLL_INTERVAL_SEC = 2.0

ROOT = BASE_DIR / "parallel_runs"
INPUT_DIR = ROOT / "inputs"
OUTPUT_DIR = ROOT / "outputs"
LOG_DIR = ROOT / "logs"
REPORT_DIR = ROOT / "reports"

for directory in [INPUT_DIR, OUTPUT_DIR, LOG_DIR, REPORT_DIR]:
    directory.mkdir(parents=True, exist_ok=True)


def build_case_tag(i_value: int, v_value: int, timelimit_sec: int) -> str:
    return f"i{i_value}v{v_value}t{timelimit_sec}"


def prepare_input_file(base_input: Path, target_input: Path, i_value: int, v_value: int, timelimit_sec: int) -> None:
    """复制基础 input.xlsx，并修改 Params 中的 SKU 数、车辆数、时间标签。"""
    shutil.copy2(base_input, target_input)

    wb = load_workbook(target_input)
    try:
        ws = wb["Params"]
        ws["B1"] = i_value
        ws["B2"] = v_value
        # B12 保持同步，便于打开 Excel 时看到该场景时间；实际主求解时间由 --main-time-limit 控制。
        ws["B12"] = timelimit_sec
        wb.save(target_input)
    finally:
        wb.close()


def launch_one_case(i_value: int, v_value: int) -> dict:
    case_tag = build_case_tag(i_value, v_value, TIME_LIMIT_SEC)

    input_file = INPUT_DIR / f"{case_tag}_input.xlsx"
    output_file = OUTPUT_DIR / f"{case_tag}.xlsx"
    log_file = LOG_DIR / f"{case_tag}.log"

    prepare_input_file(BASE_INPUT, input_file, i_value, v_value, TIME_LIMIT_SEC)

    cmd = [
        PYTHON_EXE,
        str(MAIN_SCRIPT),
        "--mode", "solve",
        "--method", "scip",
        "--input", str(input_file),
        "--output", str(output_file),
        "--main-time-limit", str(TIME_LIMIT_SEC),
    ]

    log_f = open(log_file, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        cwd=BASE_DIR,
    )

    return {
        "case_tag": case_tag,
        "i_value": i_value,
        "v_value": v_value,
        "time_limit_sec": TIME_LIMIT_SEC,
        "proc": proc,
        "log_f": log_f,
        "input_file": input_file,
        "output_file": output_file,
        "log_file": log_file,
        "return_code": None,
        "finished": False,
        "start_time": time.time(),
        "end_time": None,
    }


def finalize_job(job: dict) -> None:
    if job["finished"]:
        return

    ret = job["proc"].wait()
    job["log_f"].close()
    job["return_code"] = ret
    job["finished"] = True
    job["end_time"] = time.time()

    elapsed = job["end_time"] - job["start_time"]
    if ret == 0:
        print(f"[done] {job['case_tag']} 运行完成，用时 {elapsed:.1f}s，输出：{job['output_file']}")
    else:
        print(f"[failed] {job['case_tag']} 运行失败，返回码={ret}，请查看日志：{job['log_file']}")


def write_summary(finished: list[dict]) -> Path:
    report_file = REPORT_DIR / "batch_run_summary.csv"
    with open(report_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "case_tag",
                "i_value",
                "v_value",
                "time_limit_sec",
                "return_code",
                "elapsed_sec",
                "input_file",
                "output_file",
                "log_file",
            ],
        )
        writer.writeheader()
        for job in finished:
            elapsed = None
            if job.get("start_time") is not None and job.get("end_time") is not None:
                elapsed = job["end_time"] - job["start_time"]
            writer.writerow({
                "case_tag": job["case_tag"],
                "i_value": job["i_value"],
                "v_value": job["v_value"],
                "time_limit_sec": job["time_limit_sec"],
                "return_code": job["return_code"],
                "elapsed_sec": f"{elapsed:.3f}" if elapsed is not None else "",
                "input_file": str(job["input_file"]),
                "output_file": str(job["output_file"]),
                "log_file": str(job["log_file"]),
            })
    return report_file


def main() -> None:
    if not BASE_INPUT.exists():
        raise FileNotFoundError(f"未找到基础输入文件：{BASE_INPUT}")
    if not MAIN_SCRIPT.exists():
        raise FileNotFoundError(f"未找到主程序文件：{MAIN_SCRIPT}")

    pending = list(SCENARIOS)
    running: list[dict] = []
    finished: list[dict] = []

    print(f"[info] 共 {len(SCENARIOS)} 个场景，最大并发数 = {MAX_CONCURRENT}")
    print(f"[info] 每个场景 warm-start 注入后 SCIP 主求解时间上限 = {TIME_LIMIT_SEC} 秒")

    while pending or running:
        while pending and len(running) < MAX_CONCURRENT:
            i_value, v_value = pending.pop(0)
            job = launch_one_case(i_value, v_value)
            running.append(job)
            print(f"[started] {job['case_tag']}，日志：{job['log_file']}")

        still_running = []
        for job in running:
            ret = job["proc"].poll()
            if ret is None:
                still_running.append(job)
            else:
                finalize_job(job)
                finished.append(job)

        running = still_running
        if running:
            time.sleep(POLL_INTERVAL_SEC)

    success_count = sum(1 for job in finished if job["return_code"] == 0)
    fail_count = len(finished) - success_count
    report_file = write_summary(finished)

    print("\n全部场景已结束。")
    print(f"[summary] 成功：{success_count}，失败：{fail_count}")
    print(f"[summary] 输出目录：{OUTPUT_DIR}")
    print(f"[summary] 日志目录：{LOG_DIR}")
    print(f"[summary] 汇总报告：{report_file}")


if __name__ == "__main__":
    main()
