"""让 tests/ 能直接 import 项目根目录下的模块（config / budget / graph ...）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
