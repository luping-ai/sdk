#!/usr/bin/env python
"""检查运行环境是否就绪，并给出可操作的修复步骤。

## 为什么需要这个

跑不通的常见原因不是代码问题，而是环境问题，且症状互相矛盾：

  · 敲 python 毫无反应、退出码 9009
      → Windows 的 python 指向应用商店占位符（0 字节）
  · ModuleNotFoundError: No module named 'localai'
      → 没装包，或装到了另一个 Python 里
  · 能 import 但连不上 Runtime
      → Runtime 没启动，或 discovery 文件过期

这三种情况光看报错很难区分，尤其第一种**连 Python 代码都执行不到**，
任何 try/except 都帮不上——只能靠外部脚本或文档。

用法：

    python scripts/check_env.py
    py scripts/check_env.py

它会逐项检查并打印结论。任一项失败时退出码为 1。
"""

from __future__ import annotations

import os
import platform
import shutil
import sys


def line(label: str, value: object) -> None:
    print(f"  {label:<22}{value}")


def ok(text: str = "") -> None:
    print(f"  ✓ {text}" if text else "  ✓")


def warn(text: str) -> None:
    print(f"  ! {text}")


def bad(text: str) -> None:
    print(f"  ✗ {text}")


def main() -> int:
    problems = 0
    print()
    print("=" * 68)
    print("  Local AI Hub Python SDK —— 环境检查")
    print("=" * 68)

    # -- 1. 解释器 ---------------------------------------------------
    print("\n▸ 1. Python 解释器")
    line("版本", platform.python_version())
    line("路径", sys.executable)
    line("平台", f"{platform.system()} {platform.release()}")

    # ruff 的 UP036 会建议删掉这个版本检查（"最低版本已满足"），但那是
    # 因为它按 pyproject 的 requires-python 推断。本脚本的用途恰恰是
    # 给**在旧解释器上跑的人**看，所以检查必须保留。
    if sys.version_info < (3, 9):  # noqa: UP036
        bad(f"需要 Python 3.9+，当前 {platform.python_version()}")
        problems += 1
    else:
        ok("版本满足要求（>=3.9）")

    # 是否在 venv 里
    in_venv = (
        hasattr(sys, "real_prefix")
        or sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    )
    line("虚拟环境", "是（" + sys.prefix + "）" if in_venv else "否")

    # -- 2. Windows 的 python 别名陷阱 -------------------------------
    if platform.system() == "Windows":
        print("\n▸ 2. python 命令是否可用")
        found = shutil.which("python")
        if found is None:
            warn("PATH 里找不到 python 命令")
            print("       用 py 或完整路径运行本 SDK 的示例")
        else:
            try:
                size = os.path.getsize(found)
            except OSError:
                size = -1
            looks_alias = "WindowsApps" in found
            line("python 指向", found)
            if looks_alias or size == 0:
                bad("这是 Windows 应用商店的占位符，不是真正的 Python")
                print("       症状：敲 python 毫无反应、退出码 9009")
                print("       做法：改用 py，或直接指定解释器完整路径")
                print("             也可以到 设置 → 应用 → 高级应用设置 →")
                print("             应用执行别名，关闭 python.exe / python3.exe")
                problems += 1
            else:
                ok(f"是真实可执行文件（{size} 字节）")

        launcher = shutil.which("py")
        line("py 启动器", launcher or "未找到")
        if launcher:
            ok("可用 py 代替 python")
    else:
        print("\n▸ 2. python 命令")
        found = shutil.which("python3") or shutil.which("python")
        line("python3 指向", found or "未找到")
        if found:
            ok()
        else:
            bad("PATH 里找不到 python3")
            problems += 1

    # -- 3. 包是否可导入 ---------------------------------------------
    print("\n▸ 3. localai 包")
    try:
        import localai
    except ModuleNotFoundError as exc:
        bad(f"无法导入 localai（{exc}）")
        print("       做法：用与 python 同一个解释器安装：")
        print("             python -m pip install luping-localai")
        print("             或先激活项目的 .venv 再运行")
        problems += 1
    else:
        ok(f"已安装，版本 {localai.__version__}")
        line("安装位置", str(os.path.dirname(localai.__file__)))

    # -- 4. Runtime 是否在线 -----------------------------------------
    print("\n▸ 4. Local AI Hub Runtime")
    try:
        from localai import APIError, Client, DiscoveryPath
    except ModuleNotFoundError:
        warn("跳过（包不可导入）")
    else:
        try:
            path = DiscoveryPath()
        except Exception:
            path = "(无法确定)"
        line("discovery 文件", path)
        print(f"       {'存在' if os.path.exists(str(path)) else '不存在'}")

        env_url = os.environ.get("LAIH_URL")
        if env_url:
            line("LAIH_URL", env_url)

        try:
            c = Client()
            rep = c.health()
        except APIError as exc:
            bad(f"连不上：{exc.message}")
            tried = env_url or Client().base_url
            print(f"       尝试的地址：{tried}")
            print("       做法：启动 Runtime")
            print("             localai-cli serve")
            problems += 1
        else:
            ok(f"在线，状态 {rep.state}")
            line("地址", c.base_url)
            if rep.ready:
                ok("可以发起推理请求")
            else:
                warn(f"暂不可服务：{rep.reason or '未提供原因'}")

    # -- 汇总 --------------------------------------------------------
    print()
    print("=" * 68)
    if problems == 0:
        print("  环境就绪。可以运行：")
        print("    python examples/demo.py")
        print("=" * 68)
        return 0

    print(f"  发现 {problems} 个问题，按上面提示处理后再跑一次本脚本。")
    print("=" * 68)
    return 1


if __name__ == "__main__":
    sys.exit(main())
