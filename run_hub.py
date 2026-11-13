"""Hub 常驻入口：任务计划/开机自启用 pythonw 调本文件。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from techdaily.hub import serve

if __name__ == "__main__":
    serve()
