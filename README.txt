# BLD 顺序逐车 warmstart + SCIP 整体 MIP 工程

本工程用于 BLD 配矿/装车/分批优化：先按车辆逐车求解，构造一个完整可行初始解；再把这个解注入 SCIP，继续求解整体 MIP。

## 主要文件

- `bld_main.py`：单次运行入口。
- `bld_solver.py`：逐车 warmstart、SCIP 参数设置、整体 MIP 求解、过程日志解析。
- `bld_model.py`：完整 MIP 模型。
- `bld_data.py`：读取 `input.xlsx`。
- `bld_utils.py`：数据校验和结果写回。
- `run_parallel_experiments.py`：批量并发运行脚本，结构参考 `scip.zip`。
- `README_parallel.txt`：批量运行说明。

## 单次运行

```bash
python bld_main.py --mode solve --input input.xlsx --output output.xlsx --main-time-limit 2400
```

`--main-time-limit` 表示逐车初始解注入 SCIP 后，整体 MIP 主求解阶段的时间上限，单位秒。

如果不传该参数，默认使用 `bld_solver.py` 中的：

```python
FORCE_MAIN_MIP_TIME_LIMIT_SEC = 2400.0
```

如果想恢复为读取 Excel `Params!B12`，把它改成：

```python
FORCE_MAIN_MIP_TIME_LIMIT_SEC = None
```

并且运行时不要传 `--main-time-limit`。

## 只做预检查

```bash
python bld_main.py --mode precheck --input input.xlsx
```

## 禁用 warmstart 做冷启动对照

```bash
python bld_main.py --mode solve --disable-warmstart --input input.xlsx --output output.xlsx --main-time-limit 2400
```

## 批量运行

```bash
python run_parallel_experiments.py
```

详见 `README_parallel.txt`。
