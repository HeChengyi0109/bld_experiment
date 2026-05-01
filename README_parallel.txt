逐车求解版本：批量运行说明
============================

1. 运行方式
-----------
在当前工程目录执行：

    python run_parallel_experiments.py

Windows 下也可以双击：

    run_parallel_experiments.bat


2. 批量脚本的核心思路
---------------------
批量脚本不会重写求解逻辑，而是为每个场景生成独立 input.xlsx，
然后通过 subprocess 调用同一个 bld_main.py：

    python bld_main.py --mode solve --input <case_input.xlsx> --output <case_output.xlsx>

因此批量运行与单次运行走的是同一套逐车求解逻辑。


3. 场景配置
-----------
在 run_parallel_experiments.py 中修改：

    SCENARIOS = [
        (200, 2),
        (300, 2),
        (200, 3),
    ]

其中：
- 第一个数字写入 Params!B1，即 SKU 数量；
- 第二个数字写入 Params!B2，即车辆数量。


4. 时间参数
-----------
GLOBAL_TIME_LIMIT_SEC 写入 Params!B12，表示整个逐车流程的全局时间上限。
如果不想覆盖 input.xlsx 里的 B12，可设置：

    GLOBAL_TIME_LIMIT_SEC = None

单车时间限制接口已预留，但默认关闭：

    VEHICLE_TIME_LIMIT_SEC = None

如需启用每个车次最多 300 秒，可改为：

    VEHICLE_TIME_LIMIT_SEC = 300

启用后，批量脚本会自动给 bld_main.py 传：

    --vehicle-time-limit 300

bld_solver.py 中会对每个单车子问题设置该时间上限；如果同时存在全局剩余时间，
则实际单车时限取 min(单车上限, 全局剩余时间)。


5. 输出目录
-----------
运行后会生成：

    parallel_runs/
    ├── inputs/      每个场景独立 input.xlsx
    ├── outputs/     每个场景独立 output.xlsx
    ├── logs/        每个场景独立日志
    └── reports/     batch_run_summary.csv 汇总报告
