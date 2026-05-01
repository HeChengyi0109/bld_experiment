import shutil
import subprocess
import sys
import time
from pathlib import Path

from openpyxl import load_workbook

# ==============================
# 并行批跑配置
# i -> Params!B1 -> num_of_SKUs
# v -> Params!B2 -> num_of_Vehicles
# t -> 仅用于文件命名标签；实际求解时限由 bld_solver.py 中的 FORCE_SCIP_TIME_LIMIT_SEC 控制
# ==============================
BASE_INPUT = Path("input.xlsx")
MAIN_SCRIPT = Path("bld_main.py")
PYTHON_EXE = sys.executable

SCENARIOS = [
    # (200, 2),
    # (300, 2),
    # (200, 3),
    # (300, 3),
    (200, 4),
    (200, 5),
    (300, 10),
]

TIME_LIMIT_TAG = 300
MAX_CONCURRENT = len(SCENARIOS)
POLL_INTERVAL_SEC = 2.0

ROOT = Path("parallel_runs")
INPUT_DIR = ROOT / "inputs"
OUTPUT_DIR = ROOT / "outputs"
LOG_DIR = ROOT / "logs"
CSV_DIR = ROOT / "csv"

for directory in [INPUT_DIR, OUTPUT_DIR, LOG_DIR, CSV_DIR]:
    directory.mkdir(parents=True, exist_ok=True)


def build_case_tag(i_value: int, v_value: int, timelimit_sec: int) -> str:
    return f"i{i_value}v{v_value}t{timelimit_sec}"


def prepare_input_file(base_input: Path, target_input: Path, i_value: int, v_value: int, timelimit_sec: int):
    shutil.copy2(base_input, target_input)

    wb = load_workbook(target_input)
    ws = wb["Params"]
    ws["B1"] = i_value
    ws["B2"] = v_value
    ws["B12"] = timelimit_sec
    wb.save(target_input)
    wb.close()


def expected_csv_path(output_file: Path) -> Path:
    stem = output_file.stem
    return output_file.parent / f"{stem}_scip_progress" / f"{stem}.csv"


def launch_one_case(i_value: int, v_value: int) -> dict:
    case_tag = build_case_tag(i_value, v_value, TIME_LIMIT_TAG)

    input_file = INPUT_DIR / f"{case_tag}_input.xlsx"
    output_file = OUTPUT_DIR / f"{case_tag}.xlsx"
    log_file = LOG_DIR / f"{case_tag}.log"

    prepare_input_file(BASE_INPUT, input_file, i_value, v_value, TIME_LIMIT_TAG)

    cmd = [
        PYTHON_EXE,
        str(MAIN_SCRIPT),
        "--input", str(input_file),
        "--output", str(output_file),
    ]

    log_f = open(log_file, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        cwd=Path(__file__).resolve().parent,
    )

    return {
        "case_tag": case_tag,
        "proc": proc,
        "log_f": log_f,
        "input_file": input_file,
        "output_file": output_file,
        "log_file": log_file,
        "csv_source": expected_csv_path(output_file),
        "csv_target": CSV_DIR / f"{case_tag}.csv",
        "return_code": None,
        "finished": False,
    }


def finalize_job(job: dict):
    if job["finished"]:
        return

    ret = job["proc"].wait()
    job["log_f"].close()
    job["return_code"] = ret
    job["finished"] = True

    if ret == 0:
        csv_source = job["csv_source"]
        csv_target = job["csv_target"]
        if csv_source.exists():
            shutil.copy2(csv_source, csv_target)
            print(f"[done] {job['case_tag']} 运行完成，CSV：{csv_target}")
        else:
            print(f"[warn] {job['case_tag']} 运行完成，但未找到 CSV：{csv_source}")
    else:
        print(f"[failed] {job['case_tag']} 运行失败，返回码={ret}，请查看日志：{job['log_file']}")


def main():
    if not BASE_INPUT.exists():
        raise FileNotFoundError(f"未找到基础输入文件：{BASE_INPUT.resolve()}")
    if not MAIN_SCRIPT.exists():
        raise FileNotFoundError(f"未找到主程序文件：{MAIN_SCRIPT.resolve()}")

    pending = list(SCENARIOS)
    running = []
    finished = []

    print(f"[info] 共 {len(SCENARIOS)} 个场景，最大并发数 = {MAX_CONCURRENT}")

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
    print("\n全部场景已结束。")
    print(f"[summary] 成功：{success_count}，失败：{fail_count}")
    print(f"[summary] 汇总 CSV 目录：{CSV_DIR.resolve()}")


if __name__ == "__main__":
    main()
