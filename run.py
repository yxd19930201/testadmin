"""启动入口：把项目根目录加入 sys.path 后启动 GUI。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from captrace.app_gui import main

if __name__ == "__main__":
    main()
