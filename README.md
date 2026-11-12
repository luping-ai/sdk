# Luping AI SDKs

Luping AI 各产品对外发布的客户端 SDK，按**产品 → 语言**组织。

Apache License 2.0，见 [LICENSE](LICENSE)。

## 目录结构

```
local-ai-runtime-sdk/          Local AI Hub Runtime 的客户端
├── docs/openapi.yaml          接口契约（多语言共用，唯一真相）
└── go/                        Go 客户端
    ├── go.mod                 独立 Go 模块
    ├── localai/               源码
    └── examples/sdkdemo/      可运行示例

# 未来的产品按同样形状加在这里：
# other-product-sdk/
#   docs/
#   go/
#   python/
```

一个产品一个目录，产品内按语言分子目录。**同一产品的多语言 SDK 共享该产品
的 `docs/`**（契约、示例数据），这是放在一个仓库里的主要理由——服务端改了
接口，契约只需要改一份，各语言的实现和测试都看得见。

## 现在可用的 SDK

| 产品 | 语言 | 模块 / 包名 | 安装 |
| --- | --- | --- | --- |
| Local AI Hub Runtime | Go | `github.com/luping-ai/sdk/local-ai-runtime-sdk/go` | `go get github.com/luping-ai/sdk/local-ai-runtime-sdk/go` |

接入说明见各语言目录下的 README：

- [Local AI Hub Go SDK](local-ai-runtime-sdk/go/README.md)

## 发布：Go 模块的 tag 规则（重要）

**Go 模块在子目录里，版本 tag 必须带目录路径前缀。**

```
local-ai-runtime-sdk/go/v0.1.0     ← 正确
v0.1.0                             ← 错误，go get 会失败
```

裸 tag 失败时的报错是：

```
module github.com/luping-ai/sdk@v0.1.0 found, but does not contain
package github.com/luping-ai/sdk/local-ai-runtime-sdk/go
```

这句话不会告诉你"tag 少写了前缀"，只会让人以为包不存在或路径写错了。
所以 CI 在推 tag 时会直接拦掉裸 `v*`，并打印正确格式（见
[.github/workflows/ci.yml](.github/workflows/ci.yml) 的 `tag-format` 任务）。

发布某个 Go 模块的新版本：

```bash
git tag local-ai-runtime-sdk/go/v0.1.0
git push origin local-ai-runtime-sdk/go/v0.1.0
```

多个产品、多种语言共存时，tag 会按产品交错排列：

```
local-ai-runtime-sdk/go/v0.1.0
local-ai-runtime-sdk/go/v0.2.0
other-product-sdk/go/v1.0.0
```

`git tag -l "local-ai-runtime-sdk/*"` 可以按产品过滤。

其它语言不受这条规则约束：Python 发到 PyPI、npm 发到 registry 之后，
仓库布局对使用者完全不可见，tag 只用于标记发布点。

## 本地开发

各语言目录是**自包含**的，进去就能跑：

```bash
cd local-ai-runtime-sdk/go
go test ./...
```

不需要在仓库根做任何 bootstrap——没有工作区文件、没有根级 go.mod。

## 与服务端的关系

SDK 的对应服务端（如 Local AI Hub Runtime 本体）是**私有软件**，不在本仓库。
本仓库只包含客户端：它通过 HTTP 与本地运行的服务通信，不需要服务端源码
即可编译和测试。

## 加一个新语言的 SDK

1. 建目录 `<product>/<language>/`
2. 让它自包含（Go 需要 `go.mod`，Python 需要 `pyproject.toml`，npm 需要 `package.json`）
3. 在 CI 里加一个 job，`working-directory` 指向该目录，并按路径过滤触发
4. 到本 README 的"现在可用的 SDK"表里加一行
