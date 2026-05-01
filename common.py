"""
通用模块定义 - 可复用的表格读取函数
"""
import pandas as pd
import re
from bld_utils import parse_excel_range, col_letter_to_index

def read_cell_value(file_path, sheet_name, cell_ref):
    """
    读取Excel中特定单元格的值
    """
    try:
        # 读取整个工作表
        df = pd.read_excel(file_path, sheet_name=sheet_name, header=None)
        
        # 解析单元格引用（如"A1", "B10"等）
        col_letter = re.findall('[A-Z]+', cell_ref)[0]
        row_num = int(re.findall('[0-9]+', cell_ref)[0]) - 1  # 转换为0-based索引
        
        col_num = col_letter_to_index(col_letter)
        
        return df.iloc[row_num, col_num]
    except Exception as e:
        print(f"读取单元格 {sheet_name}!{cell_ref} 时出错: {e}")
        return None

def read_range_data(file_path, range_str):
    """
    读取Excel中指定范围的数据
    """
    try:
        sheet_name, cell_range = parse_excel_range(range_str)
        
        if ':' in cell_range:
            # 范围读取（如"A1:C10"）
            start_cell, end_cell = cell_range.split(':')
            
            # 解析起始单元格
            start_col_letter = re.findall('[A-Z]+', start_cell)[0]
            start_row = int(re.findall('[0-9]+', start_cell)[0]) - 1
            
            # 解析结束单元格
            end_col_letter = re.findall('[A-Z]+', end_cell)[0]
            end_row = int(re.findall('[0-9]+', end_cell)[0]) - 1
            
            start_col = col_letter_to_index(start_col_letter)
            end_col = col_letter_to_index(end_col_letter)
            
            # 计算行数和列数
            nrows = end_row - start_row + 1
            ncols = end_col - start_col + 1
            
            # 读取数据
            df = pd.read_excel(
                file_path, 
                sheet_name=sheet_name, 
                header=None,
                skiprows=start_row,
                nrows=nrows,
                usecols=range(start_col, end_col + 1)
            )
            
            # 如果只有一行一列，返回标量值
            if nrows == 1 and ncols == 1:
                return df.iloc[0, 0]
            # 如果只有一行，返回一维数组
            elif nrows == 1:
                return df.iloc[0].tolist()
            # 如果只有一列，返回一维数组
            elif ncols == 1:
                return df.iloc[:, 0].tolist()
            # 否则返回DataFrame
            else:
                return df
        else:
            # 单个单元格读取
            return read_cell_value(file_path, sheet_name, cell_range)
            
    except Exception as e:
        print(f"[error]读取范围 {range_str} 时出错: {e}")
        return None

