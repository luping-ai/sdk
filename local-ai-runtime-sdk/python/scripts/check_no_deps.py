#!/usr/bin/env python
"""校验 pyproject.toml 的运行时依赖为空。

## 为什么需要这个脚本

零运行时依赖是 Python SDK 的设计约束（与 Go SDK 一致）：SDK 会被大量
第三方项目引入，任何依赖都会传染给它们，并可能撞版本。

约束只写在 README 里是不够的——某天有人为了一个便利函数加了
`requests`，没人会注意到，直到下游用户的依赖树被污染。所以放在 CI 里
当硬门禁。

## 为什么是独立文件而不是内联在 workflow 里

最初把这段逻辑直接写在 `ci.yml` 的 `run: |` 块里，用
`python -c "..."` 传递多行代码。**那是坏的**：bash 会把换行与缩进原样
传给 Python，`python -c` 收到的是

    <换行>
              import tomllib
              with open(...) as f:

第一行空、每行带缩进，于是 IndentationError。`||` 后面的 fallback
（tomli）也同样失败，最终 `$count` 是空串，`"" != "0"` 成立，CI 报
"dependencies 应为空"——一个完全误导的失败信息。

写成独立文件后：没有 shell 引号嵌套、本地可复现、逻辑能写完整
（这里顺带校验了更根本的东西：包能否被正确识别）。

用法：

    python scripts/check_no_deps.py            # 从任意目录运行
"""

from __future__ import annotations

import sys
from pathlib import Path

ALLOWED: list = []


def load_pyproject(path: Path) -> dict:
    """读 pyproject.toml，兼容 3.9/3.10（无 tomllib）。"""
    try:
        import tomllib  # 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            sys.exit(
                "需要 tomllib（Python 3.11+）或 tomli。"
                "在 CI 里用 'pip install tomli' 安装后备实现。"
            )

    with open(path, "rb") as f:
        return tomllib.load(f)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    pyproject = root / "pyproject.toml"

    if not pyproject.is_file():
        print(f"找不到 {pyproject}", file=sys.stderr)
        return 1

    doc = load_pyproject(pyproject)
    project = doc.get("project")
    if not isinstance(project, dict):
        print("pyproject.toml 缺少 [project] 段", file=sys.stderr)
        return 1

    deps = project.get("dependencies", [])
    if deps != ALLOWED:
        print("运行时依赖必须为空（零依赖是设计约束）", file=sys.stderr)
        print(f"  实际：{deps}", file=sys.stderr)
        print("", file=sys.stderr)
        print("  如果你确实需要加依赖，先想清楚代价：SDK 会被大量第三方",
              file=sys.stderr)
        print("  项目引入，任何依赖都会传染给它们，并可能与它们已有的版本",
              file=sys.stderr)
        print("  冲突。Go SDK 同样是零依赖。", file=sys.stderr)
        print("", file=sys.stderr)
        print("  标准库通常够用：urllib（HTTP）、json、socket、threading。",
              file=sys.stderr)
        return 1

    # 顺带确认包能被 setuptools 找到（拼错 packages 会导致 wheel 里没代码）
    packages = doc.get("tool", {}).get("setuptools", {}).get("packages")
    if packages != ["localai"]:
        print(f"packages 应为 ['localai']，实际 {packages}",
              file=sys.stderr)
        return 1

    init = root / "localai" / "__init__.py"
    if not init.is_file():
        print(f"找不到 {init}", file=sys.stderr)
        return 1

    # py.typed 缺失时，下游 mypy 不会使用本包的类型注解——静默失效
    pytyped = root / "localai" / "py.typed"
    if not pytyped.is_file():
        print(f"找不到 {pytyped}（缺它会让下游 mypy 忽略本包类型）",
              file=sys.stderr)
        return 1

    print("OK 零运行时依赖")
    print(f"OK packages = {packages}")
    print("OK localai/__init__.py 与 py.typed 就位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
