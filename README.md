# MCP-TY

基于 [ty](https://github.com/astral-sh/ty) 类型检查器的 MCP 服务器，为 AI 提供精确的 Python 代码语义分析能力。

## 为什么选择 ty？

- **极速**：基于 Rust 的增量分析（Salsa 架构），毫秒级响应
- **低内存**：可同时分析多个项目而不会撑爆内存
- **精准**：语义级别的代码理解，而不是简单的文本搜索
- **与 uv 集成**：自动识别虚拟环境和依赖

## 返回格式

所有工具返回 JSON 格式的结构化数据：

```json
// 成功
{"status": "ok", "data": {...}}

// 错误
{"status": "error", "message": "..."}

// 未找到
{"status": "not_found", "message": "..."}
```

## 功能特性

### 符号搜索工具（替代原生搜索）

| 工具 | 功能 | LSP 接口 |
|------|------|----------|
| `search_symbol` | **关键词搜索符号**，返回文件名+行号 | `workspace/symbol` |
| `list_file_symbols` | 列出文件中的所有符号结构 | `textDocument/documentSymbol` |

### 精确定位工具

| 工具 | 功能 | LSP 接口 |
|------|------|----------|
| `start_project` | 初始化项目分析 | `initialize` |
| `get_definition` | 跳转到符号定义 | `textDocument/definition` |
| `find_usages` | 查找所有引用 | `textDocument/references` |
| `get_type_info` | 获取类型信息 | `textDocument/hover` |
| `get_diagnostics` | 获取类型错误 | `textDocument/publishDiagnostics` |
| `get_completions` | 代码补全建议 | `textDocument/completion` |
| `analyze_file` | 文件综合分析 | 多个接口组合 |

### 代码编辑工具

| 工具 | 功能 | LSP 接口 |
|------|------|----------|
| `safe_rename` | 跨文件安全重命名符号 | `textDocument/rename` |
| `get_code_actions` | 获取可用的快速修复 | `textDocument/codeAction` |
| `apply_code_action` | 应用指定的代码操作 | `textDocument/codeAction` |
| `get_edit_preview` | 预览代码修改内容 | `textDocument/codeAction` |

**为什么代码编辑功能很重要？**

传统 AI 修改代码的方式是"生成新代码"，这容易产生幻觉或遗漏。
而基于 ty 的编辑是**语义级精确计算**：

```
AI 发指令 → ty 分析类型系统 → 计算精确的 diff → 应用修改
```

这种方式的优势：
- 重命名时找到所有**真实引用**，不会漏改或错改
- 自动修复知道如何添加正确的 import
- 修改是可预览的，AI 可以先看再决定是否应用

## 安装

### 前置条件

确保已安装 [ty](https://github.com/astral-sh/ty)：

```bash
# 使用 uv 安装
uv tool install ty

# 或使用 pipx
pipx install ty
```

### 安装 mcp-ty

```bash
# 克隆项目
git clone https://github.com/yourusername/mcp-ty.git
cd mcp-ty

# 使用 uv 安装
uv sync
```

## 配置

### Cursor 配置

在 Cursor 的 MCP 配置文件中添加：

```json
{
  "mcpServers": {
    "ty-context-engine": {
      "command": "uv",
      "args": [
        "--directory",
        "D:/PythonProjectAll/mcp-ty",
        "run",
        "mcp-ty"
      ]
    }
  }
}
```

### Claude Desktop 配置

编辑 `claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "ty-context-engine": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/mcp-ty",
        "run",
        "mcp-ty"
      ]
    }
  }
}
```

## 使用示例

### 1. 初始化项目

首先，让 AI 初始化要分析的项目：

```
请初始化项目 D:/MyPythonProject 进行类型分析
```

AI 会调用 `start_project` 工具。

### 2. 跳转到定义

```
在 src/models/user.py 的第 45 行第 10 列，帮我找到这个函数的定义
```

### 3. 查找所有引用

```
找出 User 类在整个项目中的所有使用位置
```

### 4. 获取类型信息

```
告诉我 src/services/auth.py 第 23 行的 session 变量是什么类型
```

### 5. 检查类型错误

```
检查 src/api/routes.py 有没有类型错误
```

## 架构说明

```
┌─────────────┐     stdio      ┌─────────────┐     JSON-RPC     ┌─────────┐
│  AI Client  │ ◄────────────► │  MCP Server │ ◄───────────────► │   ty    │
│  (Cursor)   │                │  (FastMCP)  │                   │ server  │
└─────────────┘                └─────────────┘                   └─────────┘
```

- **AI Client**：Cursor、Claude Desktop 等支持 MCP 的 AI 客户端
- **MCP Server**：本项目，作为桥梁连接 AI 和 ty
- **ty server**：Astral 开发的高性能 Python 类型检查器 LSP 服务

## 与传统搜索的区别

| 场景 | grep/文本搜索 | ty 语义分析 |
|------|---------------|-------------|
| 搜索 `User` | 返回几千个匹配 | 只返回 `class User` 的定义 |
| 查找方法调用 | 可能匹配注释和字符串 | 只匹配真正的方法调用 |
| 理解类型 | 无法推断 | 精确显示推断类型 |
| 重构安全性 | 容易改错 | 找到所有真实引用 |

## 局限性

ty 提供的是**代码结构语义**，不是**自然语言语义**：

- 能做：找到 `process_order` 函数的所有调用链
- 不能做：理解"帮我找处理退款的代码"（除非函数名就叫退款）

**建议**：将 ty 的结构化检索与 RAG 向量搜索结合，是目前最强的 AI 代码理解方案。

## 开发

```bash
# 安装开发依赖
uv sync --dev

# 运行测试
uv run pytest

# 类型检查
uv run ty check
```

## License

MIT

