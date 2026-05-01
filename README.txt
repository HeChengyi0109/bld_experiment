一：
Python 版本：3.11.14


二：第三方库依赖：
pyscipopt==4.2
pandas>=1.5
openpyxl>=3.1

pyscipopt为SCIP 优化器的 Python 接口，用于建模与求解

三.系统级依赖
SCIP 优化器版本：9.2.4（64-bit）
SCIP 并非纯 Python 包，需要先完成系统级安装与配置。
下载后将SCIP 的 bin 目录在 PATH 中，再安装pyscipopt库，以确保 Python 能正确加载 SCIP 的动态库。
官方预编译版本下载链接：https://www.scipopt.org/index.php#download

四.已验证环境组合
OS        : Windows 64-bit
Python    : 3.11.14
pyscipopt : 4.2
SCIP      : 9.2.4
