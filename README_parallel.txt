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
GLOBAL_TIME_LIMIT_SEC 写入 Params!B12，表示整个逐车流程的全局总预算。
求解时会使用动态平均分配策略：

    第 k 辆车预算 = (总预算 - 已用时间) / 剩余车辆数

例如总预算 300 秒、车辆数为 v：
- 第 1 辆车预算 = 300 / v；
- 第 2 辆车预算 = (300 - 第 1 辆实际用时) / (v - 1)；
- 后续车辆以此类推。

如果不想覆盖 input.xlsx 里的 B12，可设置：

    GLOBAL_TIME_LIMIT_SEC = None

单车附加时间上限接口已预留，但默认关闭：

    VEHICLE_TIME_LIMIT_SEC = None

如需让任何单车最多 300 秒，可改为：

    VEHICLE_TIME_LIMIT_SEC = 300

启用后，批量脚本会自动给 bld_main.py 传：

    --vehicle-time-limit 300

实际单车时限取 min(动态平均预算, VEHICLE_TIME_LIMIT_SEC)。


5. 输出目录
-----------
运行后会生成：

    parallel_runs/
    ├── inputs/      每个场景独立 input.xlsx
    ├── outputs/     每个场景独立 output.xlsx
    ├── logs/        每个场景独立日志
    └── reports/     batch_run_summary.csv 汇总报告

[2026-04-29 timelimit-force 说明]
- bld_solver.py 默认设置 APPLY_SINGLE_VEHICLE_GAP_LIMITS = False，因此逐车子问题不会因为 Params!B10/B11 的 gap/absgap 达标而提前停止。
- 单车仍然使用动态分配的 limits/time：第 k 辆车预算 = (总预算 - 已用时间) / 剩余车辆数。
- 注意：SCIP 若提前证明 optimal / infeasible，仍可能在时间上限前自然结束；SCIP 的 limits/time 本质是最大运行时间，不是最小运行时间。
- 如果为了实验统计需要严格占满墙钟时间，可把 FILL_ALLOCATED_TIME_AFTER_EARLY_STOP 改为 True；这只会 sleep 补时间，不会继续优化。
