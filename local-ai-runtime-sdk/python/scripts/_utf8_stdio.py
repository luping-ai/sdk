"""把标准输出/错误切到 UTF-8（Windows 非 UTF-8 终端的兜底）。

## 为什么需要这个文件

本项目的所有脚本与示例都打印**中文**。而 Windows 控制台的默认编码
不一定是 UTF-8：

    中文系统   cp936（GBK）
    英文系统   cp1252
    GitHub Actions 的 windows-latest   cp1252

在 cp1252 终端下 `print("零运行时依赖")` 会直接抛

    UnicodeEncodeError: 'charmap' codec can't encode characters

表现为"双击运行就闪退"，而报错信息与真实原因（编码）看起来毫无关系。

这不是理论问题：CI 里 `python examples/demo.py --list` 在三个 Windows
版本上全部失败，而 Linux/macOS 都通过——差异只在默认编码。

## 为什么每个入口都要自己调一次

脚本要在**未安装包**时也能运行（check_no_deps 的用途正是检查包本身），
所以不能依赖 import localai 的副作用。这个模块与脚本同级，
`from _utf8_stdio import ensure_utf8_stdio` 在 `python scripts/xxx.py`
下能直接找到（脚本所在目录自动进 sys.path）。

## 为什么用 reconfigure 而不是包一层

`sys.stdout.reconfigure(encoding="utf-8")` 改的是底层 TextIOWrapper
的编码，不替换流对象。包一层（codecs.getwriter）会让
`contextlib.redirect_stdout`、`isatty()` 之类的检查失真。
"""

from __future__ import annotations

import contextlib
import sys


def ensure_utf8_stdio() -> None:
    """把 stdout/stderr 的编码切到 UTF-8。

    幂等：重复调用无害。已是 UTF-8 时 reconfigure 是空操作。

    errors="replace" 是兜底：即便终端仍显示不了某些字符，也只会出现
    占位符而不是崩掉——"看到大半内容"远好于"什么都看不到"。
    """
    for stream in (sys.stdout, sys.stderr):
        # 被重定向到 StringIO（pytest 捕获、测试替身）时没有 reconfigure
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            # ValueError: 流已关闭
            # OSError: 底层 fd 不支持改编码（极少见）
            reconfigure(encoding="utf-8", errors="replace")
