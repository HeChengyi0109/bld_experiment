"""
逐车求解版本的批量场景运行脚本。

设计原则：
1. 不重写求解逻辑；每个场景都通过 subprocess 调用同一个 bld_main.py。
2. 每个场景生成独立 input/output/log，避免并行写文件冲突。
3. 默认不强制每个车次的求解时间；如需启用，可设置 VEHICLE_TIME_LIMIT_SEC。
"""

import csv
import shutil
import subprocess
import sys
import time
from pathlib import Path

from openpyxl import load_workbook

# ==============================
# 路径配置：全部基于脚本所在目录，避免从其他目录启动时找不到文件
# ==============================
BASE_DIR = Path(__file__).resolve().parent
BASE_INPUT = BASE_DIR / "input.xlsx"
MAIN_SCRIPT = BASE_DIR / "bld_main.py"
PYTHON_EXE = sys.executable

ROOT = BASE_DIR / "parallel_runs"
INPUT_DIR = ROOT / "inputs"
OUTPUT_DIR = ROOT / "outputs"
LOG_DIR = ROOT / "logs"
REPORT_DIR = ROOT / "reports"

for directory in [INPUT_DIR, OUTPUT_DIR, LOG_DIR, REPORT_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

# ==============================
# 批量场景配置
# i -> Params!B1 -> num_of_SKUs
# v -> Params!B2 -> num_of_Vehicles
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

# 写入 Params!B12，表示整个逐车流程的全局时间上限。
# 若不想改 input.xlsx 里的 B12，可设为 None。
GLOBAL_TIME_LIMIT_SEC = 300

# 预留接口：是否给“每个车次的单车 MIP”单独设置时间上限。
# 默认不启用，保持原代码逻辑：每辆车使用 Params!B12 的全局剩余时间。
# 如需启用，例如每车最多 300 秒，改为：VEHICLE_TIME_LIMIT_SEC = 300
VEHICLE_TIME_LIMIT_SEC = None

# 并发数。逐车 SCIP 也会占 CPU/内存，不建议过大。
MAX_CONCURRENT = min(4, len(SCENARIOS))
POLL_INTERVAL_SEC = 2.0


def build_case_tag(i_value: int, v_value: int) -> str:
    if GLOBAL_TIME_LIMIT_SEC is None:
        time_tag = "tbase"
    else:
        time_tag = f"t{int(GLOBAL_TIME_LIMIT_SEC)}"

    if VEHICLE_TIME_LIMIT_SEC is None:
        return f"i{i_value}v{v_value}{time_tag}"
    return f"i{i_value}v{v_value}{time_tag}_vt{int(VEHICLE_TIME_LIMIT_SEC)}"


def prepare_input_file(base_input: Path, target_input: Path, i_value: int, v_value: int):
    shutil.copy2(base_input, target_input)

    wb = load_workbook(target_input)
    ws = wb["Params"]
    ws["B1"] = i_value
    ws["B2"] = v_value
    if GLOBAL_TIME_LIMIT_SEC is not None:
        ws["B12"] = float(GLOBAL_TIME_LIMIT_SEC)
    wb.save(target_input)
    wb.close()


def build_command(input_file: Path, output_file: Path) -> list[str]:
    cmd = [
        PYTHON_EXE,
        str(MAIN_SCRIPT),
        "--mode", "solve",
        "--input", str(input_file),
        "--output", str(output_file),
    ]

    # 预留接口：默认不启用。若上方 VEHICLE_TIME_LIMIT_SEC 设置为正数，则自动传给 bld_main.py。
    if VEHICLE_TIME_LIMIT_SEC is not None and float(VEHICLE_TIME_LIMIT_SEC) > 0:
        cmd.extend(["--vehicle-time-limit", str(float(VEHICLE_TIME_LIMIT_SEC))])

    return cmd


def launch_one_case(i_value: int, v_value: int) -> dict:
    case_tag = build_case_tag(i_value, v_value)

    input_file = INPUT_DIR / f"{case_tag}_input.xlsx"
    output_file = OUTPUT_DIR / f"{case_tag}.xlsx"
    log_file = LOG_DIR / f"{case_tag}.log"

    prepare_input_file(BASE_INPUT, input_file, i_value, v_value)
    cmd = build_command(input_file, output_file)

    log_f = open(log_file, "w", encoding="utf-8")
    log_f.write("[command] " + " ".join(cmd) + "\n\n")
    log_f.flush()

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


def finalize_job(job: dict):
    if job["finished"]:
        return

    ret = job["proc"].wait()
    job["log_f"].close()
    job["return_code"] = ret
    job["finished"] = True
    job["end_time"] = time.time()

    runtime = job["end_time"] - job["start_time"]
    if ret == 0:
        print(f"[done] {job['case_tag']} 运行完成，用时 {runtime:.1f}s，输出：{job['output_file']}")
    else:
        print(f"[failed] {job['case_tag']} 运行失败，返回码={ret}，请查看日志：{job['log_file']}")


def write_summary(jobs: list[dict]) -> Path:
    summary_file = REPORT_DIR / "batch_run_summary.csv"
    fields = [
        "case_tag",
        "num_of_SKUs",
        "num_of_Vehicles",
        "global_time_limit_sec",
        "vehicle_time_limit_sec",
        "return_code",
        "runtime_sec",
        "input_file",
        "output_file",
        "log_file",
        "output_exists",
    ]

    with open(summary_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for job in jobs:
            runtime = None
            if job.get("start_time") is not None and job.get("end_time") is not None:
                runtime = job["end_time"] - job["start_time"]
            writer.writerow({
                "case_tag": job["case_tag"],
                "num_of_SKUs": job["i_value"],
                "num_of_Vehicles": job["v_value"],
                "global_time_limit_sec": GLOBAL_TIME_LIMIT_SEC,
                "vehicle_time_limit_sec": VEHICLE_TIME_LIMIT_SEC,
                "return_code": job["return_code"],
                "runtime_sec": "" if runtime is None else f"{runtime:.3f}",
                "input_file": str(job["input_file"]),
                "output_file": str(job["output_file"]),
                "log_file": str(job["log_file"]),
                "output_exists": job["output_file"].exists(),
            })

    return summary_file


def main():
    if not BASE_INPUT.exists():
        raise FileNotFoundError(f"未找到基础输入文件：{BASE_INPUT}")
    if not MAIN_SCRIPT.exists():
        raise FileNotFoundError(f"未找到主程序文件：{MAIN_SCRIPT}")

    pending = list(SCENARIOS)
    running = []
    finished = []

    print(f"[info] 共 {len(SCENARIOS)} 个场景，最大并发数 = {MAX_CONCURRENT}")
    print(f"[info] 全局时间上限 Params!B12 = {GLOBAL_TIME_LIMIT_SEC}")
    print(f"[info] 单车时间上限接口 VEHICLE_TIME_LIMIT_SEC = {VEHICLE_TIME_LIMIT_SEC}")

    while pending or running:
        while pending and len(running) < MAX_CONCURRENT:
            i_value, v_value = pending.pop(0)
            job = launch_one_case(i_value, v_value)
            running.append(job)
            print(f"[started] {job['case_tag']}")

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
    summary_file = write_summary(finished)

    print("\n全部场景已结束。")
    print(f"[summary] 成功：{success_count}，失败：{fail_count}")
    print(f"[summary] 汇总报告：{summary_file}")


if __name__ == "__main__":
    main()
