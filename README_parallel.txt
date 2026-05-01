并行批跑说明
================

1. 当前默认场景写在 run_parallel_experiments.py 中：
   SCENARIOS = [
       (200, 2),
       (300, 2),
       (200, 3),
   ]

2. 直接运行：
   python run_parallel_experiments.py
   或双击：run_parallel_experiments.bat

3. 文件含义：
   i -> Params!B1 -> num_of_SKUs
   v -> Params!B2 -> num_of_Vehicles
   t -> 文件命名标签，当前是 2400

4. 输出位置：
   parallel_runs/inputs/   每个场景复制后的输入文件
   parallel_runs/outputs/  每个场景的 Excel 输出与各自 progress 目录
   parallel_runs/logs/     每个场景的控制台日志
   parallel_runs/csv/      汇总后的 CSV，文件名如 i200v2t2400.csv

5. 当前已关闭过程画图，只保留 CSV。

6. 如果你以后想一次并行跑 5 个场景，只需要改 SCENARIOS 列表；
   如果电脑带不动，可以把 MAX_CONCURRENT 调小。
