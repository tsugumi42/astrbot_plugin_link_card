"""按 AstrBot 的方式把插件当成包加载：同目录模块之间必须用相对导入（main 要真的 astrbot，不在这里）。

别的测试把插件目录放进 sys.path，`import render` 这种写法在那里能过，到了 AstrBot 里就找不到模块
（2026-11-20 推特卡片出图就是这样失败的）。所以这里另起一个进程，sys.path 只放插件的上一级目录。
"""

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARENT, NAME = os.path.split(ROOT)

SCRIPT = f"""
import sys
sys.path[:] = [p for p in sys.path if p not in ("", {ROOT!r})]
sys.path.insert(0, {PARENT!r})
sys.path.insert(0, {os.path.join(ROOT, "tests")!r})
from _stub import install
install()
sys.path.remove({os.path.join(ROOT, "tests")!r})
import importlib
for mod in ("fetchers", "cnvideo", "render", "layouts", "xcard"):
    importlib.import_module({NAME!r} + "." + mod)
print("ok")
"""


class TestPackageImport(unittest.TestCase):
    def test_every_module_imports_as_package(self):
        r = subprocess.run([sys.executable, "-I", "-c", SCRIPT], capture_output=True, text=True,
                           cwd=PARENT, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn("ok", r.stdout)


if __name__ == "__main__":
    unittest.main()
