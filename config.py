"""
配置文件 - 定义常量、参数和文件路径
"""
# 文件路径配置
INPUT_FILE = "input.xlsx"
OUTPUT_FILE = "output.xlsx"

# 求解器参数默认值
DEFAULT_TIMELIMIT = 200
DEFAULT_REL_GAP = 0.005
DEFAULT_ABS_GAP = 0.01

# Excel相关配置
DEFAULT_SHEET_NAME = "Params"

# 过程输出配置
# False: 只输出 CSV，不生成过程图
# True:  输出 CSV 和过程图
ENABLE_PROGRESS_PLOTS = False
