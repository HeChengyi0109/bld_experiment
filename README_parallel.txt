# 批量运行说明：逐车 warmstart + 整体 MIP

这个版本的结构参考 scip.zip：批量脚本不重写求解逻辑，只是生成不同场景的 input，然后多次调用同一个 bld_main.py。

## 单次运行

```bash
python bld_main.py --mode solve --input input.xlsx --output output.xlsx --main-time-limit 2400
```

含义：
1. 先按车辆逐车求解，构造 warmstart 初始解；
2. 把逐车解注入 SCIP；
3. 对注入后的整体 MIP 主求解阶段强制设置 2400 秒时间上限。

如果不传 `--main-time-limit`，程序会使用 `bld_solver.py` 中的 `FORCE_MAIN_MIP_TIME_LIMIT_SEC`。

## 批量运行

```bash
python run_parallel_experiments.py
```

Windows 下也可以双击：

```text
run_parallel_experiments.bat
```

## 修改场景

打开 `run_parallel_experiments.py`，修改：

```python
SCENARIOS = [
    (200, 2),
    (300, 2),
    (200, 3),
]
```

其中 `(i, v)` 表示：
- `Params!B1 = i`，SKU 数；
- `Params!B2 = v`，车辆数。

## 修改 warmstart 注入后的主 MIP 运行时间

修改：

```python
MAIN_TIME_LIMIT_SEC = 2400
```

这个时间只作用于“逐车初始解注入 SCIP 后”的整体 MIP 主求解阶段。逐车 warmstart 阶段耗时不会从这里扣减。

## 预留接口：逐车 warmstart 阶段时间

默认不覆盖 Excel 里的 `Params!B12`：

```python
WARMSTART_TIME_LIMIT_SEC = None
```

如果需要批量统一覆盖逐车 warmstart 阶段使用的 `Params!B12`，可以改成：

```python
WARMSTART_TIME_LIMIT_SEC = 2400
```

## 输出目录

运行后生成：

```text
parallel_runs/
├── inputs/      每个场景的 input.xlsx
├── outputs/     每个场景的 output.xlsx，以及 *_scip_progress/
├── logs/        每个场景的控制台日志和 SCIP 原生日志副本
├── csv/         每个场景的过程 CSV 副本
└── reports/     batch_run_summary.csv
```

## 冷启动对照

如需批量禁用 warmstart 做对照实验，在 `run_parallel_experiments.py` 中改：

```python
DISABLE_WARMSTART = True
```


补充：warmstart 逐车阶段时间分配
--------------------------------
如果 WARMSTART_TIME_LIMIT_SEC 或 input.xlsx 的 Params!B12 大于 0，warmstart 阶段会使用动态平均分配策略：

    第 k 辆车预算 = (总预算 - 已用时间) / 剩余车辆数

例如总预算 300 秒、车辆数为 v：第 1 辆车预算为 300/v，第 2 辆车预算为
(300 - 第 1 辆实际用时)/(v - 1)，后续车辆以此类推。

还预留了单车附加上限接口：

    WARMSTART_VEHICLE_TIME_LIMIT_SEC = None

如果改为 300，则批量脚本会给 bld_main.py 传 --vehicle-time-limit 300，
实际单车时限取 min(动态平均预算, WARMSTART_VEHICLE_TIME_LIMIT_SEC)。

[2026-04-29 timelimit-force 说明]
- bld_solver.py 默认设置 APPLY_WARMSTART_VEHICLE_GAP_LIMITS = False，因此 warmstart 阶段的逐车子问题不会因为 Params!B10/B11 的 gap/absgap 达标而提前停止。
- warmstart 单车仍然使用动态分配的 limits/time：第 k 辆车预算 = (warmstart 总预算 - 已用时间) / 剩余车辆数。
- 整体 MIP 主求解阶段仍由 MAIN_TIME_LIMIT_SEC / --main-time-limit 控制，且默认 APPLY_MAIN_MIP_GAP_LIMITS = False。
- 注意：SCIP 若提前证明 optimal / infeasible，仍可能在时间上限前自然结束；SCIP 的 limits/time 本质是最大运行时间，不是最小运行时间。
- 如果为了实验统计需要严格占满墙钟时间，可把 FILL_WARMSTART_VEHICLE_TIME_AFTER_EARLY_STOP 改为 True；这只会 sleep 补时间，不会继续优化。
