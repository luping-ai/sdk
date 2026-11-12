#!/usr/bin/env python
"""校验构建出的 wheel 结构正确。

## 检查什么，以及为什么

| 项 | 缺失的后果 |
| --- | --- |
| `localai/py.typed` | 下游 mypy **静默忽略**本包类型，用户拿不到报错 |
| `*.dist-info/licenses/LICENSE` | Apache-2.0 要求随分发附带许可证 |
| 恰好一个 wheel | 多个通常是残留，会误发旧版本 |
| 全部源码模块在内 | `packages` 配错时 wheel 缺文件，装完才发现 |

## 为什么是独立文件

最初这段逻辑内联在 `ci.yml` 的 `run: |` 里用 heredoc 传给 python。
那种写法依赖"YAML 块标量的缩进恰好等于 heredoc 内容缩进"这个隐含前提
——缩进一变就悄悄失效（或以误导的方式失败）。独立脚本没有这个问题，
而且本地能跑：

    python -m build --wheel --outdir dist
    python scripts/check_wheel.py
"""

from __future__ import annotations

import glob
import sys
import zipfile
from pathlib import Path

# 必须出现在 wheel 里的模块（与 localai/ 下的实际文件对应）
REQUIRED_MODULES = [
    "localai/__init__.py",
    "localai/_client.py",
    "localai/_discovery.py",
    "localai/_errors.py",
    "localai/_stream.py",
    "localai/_transport.py",
    "localai/_types.py",
    "localai/py.typed",
]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    wheels = sorted(glob.glob(str(root / "dist" / "*.whl")))

    if not wheels:
        print("dist/ 下没有 wheel，先运行 python -m build --wheel", file=sys.stderr)
        return 1
    if len(wheels) > 1:
        print(
            f"dist/ 下有 {len(wheels)} 个 wheel，无法确定发哪个（多半是残留）：",
            file=sys.stderr,
        )
        for w in wheels:
            print(f"  {w}", file=sys.stderr)
        print("清理 dist/ 后重新构建。", file=sys.stderr)
        return 1

    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()

    problems = 0

    missing = [m for m in REQUIRED_MODULES if m not in names]
    if missing:
        print("wheel 缺少模块：", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        print("检查 pyproject.toml 的 [tool.setuptools].packages。", file=sys.stderr)
        problems += 1

    if not any("licenses/LICENSE" in n or n.endswith(".dist-info/LICENSE")
               for n in names):
        print("wheel 缺少 LICENSE（Apache-2.0 要求随分发附带）", file=sys.stderr)
        problems += 1

    # 意外打进去的构建垃圾
    junk = [n for n in names if "__pycache__" in n or n.endswith(".pyc")
            or ".egg-info" in n]
    if junk:
        print("wheel 含构建垃圾：", file=sys.stderr)
        for n in junk[:5]:
            print(f"  {n}", file=sys.stderr)
        problems += 1

    if problems:
        return 1

    print(f"OK wheel 结构正确：{Path(wheel).name}")
    print(f"OK {len(REQUIRED_MODULES)} 个模块，含 py.typed 与 LICENSE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
