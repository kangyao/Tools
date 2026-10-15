# Lua 格式检查与格式化

Python 桌面工具，选择目录后递归扫描 Lua 文件，查看格式问题的原因、行号和修改预览，然后按自定义规则格式化选中文件或全部问题文件。附带命令行入口，适合批量检查和 CI。

## 启动

需要 **Python 3.12+（包含 Tkinter）**，无需安装 Python 第三方依赖。

双击 [LuaFormatTool.bat](LuaFormatTool.bat)，或者运行：

```powershell
python D:\git\skills\Tools\LuaFormatTool\lua_format_tool.py
```

首次使用点击 **安装 StyLua**，下载官方固定版本 **2.6.0**，并用官方发行包的 SHA-256 校验。安装到本工具的 `bin` 目录，之后可离线使用。也可选择已有 StyLua 2.6.0 可执行文件，或使用 PATH 中的 `stylua`。

命令行安装：

```powershell
python D:\git\skills\Tools\LuaFormatTool\install_stylua.py
```

下载受网络或代理限制时，可从 [StyLua 2.6.0 官方发布页](https://github.com/JohnnyMorganz/StyLua/releases/tag/v2.6.0) 获取可执行文件，再在界面中指定路径。

## 界面使用

1. 选择 Lua 目录或单个文件；排除规则使用逗号分隔。点击 **格式配置** 打开独立面板，在面板中设置格式规则、源文件编码、是否包括 `.luau` 以及是否备份，点击 **应用** 保存；**取消**、Esc 或关闭面板会放弃未应用的修改。主界面显示当前规则摘要。
2. 点击 **开始扫描**。左侧逐个显示问题文件，可以关闭“仅显示问题”查看全部扫描结果。
3. 点选文件，在右侧查看 **问题原因 / 修改差异 / 原始内容 / 格式化预览**。原因包含行号、规则、原始片段和目标片段。**修改差异** 默认使用左右对比：左侧原始内容、右侧格式化预览，分别显示源文件行号；红色标出修改前 / 删除行，绿色标出修改后 / 新增行，灰色空位保持两边对齐。两侧纵向同步滚动，横向可独立滚动，支持 **上一处 / 下一处** 跳转；也可切换为 **单栏差异**。仅换行符或末尾换行不同时会显示 `[CRLF]`、`[LF]` 或 `[无换行]` 标记（这些标记只用于预览）。
4. 可多选文件后点击 **格式化选中文件**，也可以点击 **格式化全部问题文件**。成功写入后立即重新检查。
5. 格式配置面板中的 **导入规则 / 导出规则** 使用 StyLua TOML，导入后点击 **应用** 才生效；主界面的 **导出扫描报告** 输出包含原因和差异的 JSON。

修改扫描路径、格式规则、源文件编码或排除规则后，需要重新扫描才能格式化，确保写入的是所查看的预览。扫描和格式化在后台运行，**停止** 会在当前文件完成后停止处理后续文件。

工具记住上次路径和规则，保存在本目录的 `settings.local.json`；此文件和下载的 StyLua 不纳入 Git。

## 可设置的格式规则

| 规则 | 工具默认值 | 作用 |
| --- | --- | --- |
| Lua 版本 | Lua54 | Lua51 / Lua52 / Lua53 / Lua54 / LuaJIT / Luau / CfxLua / All |
| 缩进类型与宽度 | Spaces / 4 | 空格或 Tab；Tab 宽度用于布局估算 |
| 参考行宽 | 120 | 换行参考宽度，长字符串或注释可能超过此值 |
| 换行符 | Unix | LF；Windows 使用 CRLF |
| 字符串引号 | AutoPreferDouble | 优先双引号、优先单引号、强制双引号或强制单引号 |
| 调用括号 | Always | 单字符串/表参数调用的括号风格 |
| 合并简单语句 | Never | 简单函数和条件是否合并到一行 |
| 函数名后空格 | Never | 定义、调用或两者添加空格 |
| 块首尾空行 | Never | 是否保留块首尾额外空行 |

配置格式和参数含义参见 [StyLua 官方文档](https://github.com/JohnnyMorganz/StyLua#configuration)。导入 TOML 时，未指定的字段遵循 StyLua 默认值（包括 Tab 缩进和 All 语法）；新建工具配置采用上表默认值。

扫描只使用界面设置或显式传入的 `--config`，不会自动读取目标目录的 `.stylua.toml`、`.editorconfig`、`.gitignore` 或 `.styluaignore`。项目已有规则时请先导入，目录和文件过滤通过工具的排除规则设置。`require` 排序始终关闭；导入启用排序的配置会明确报错。

## 命令行

下面命令在本工具目录运行，路径可以使用绝对路径。

```powershell
# 只读扫描，显示每个问题的行号与原因
python lua_format_tool.py scan D:\Projects\Scripts

# 查看修改差异，导出报告，排除 vendor 和类型声明
python lua_format_tool.py scan D:\Projects\Scripts --diff --json report.json --exclude vendor --exclude "**/*.d.lua"

# 创建自定义规则文件（文件已存在时不覆盖）
python lua_format_tool.py init-config stylua.toml

# 使用配置文件，单次覆盖缩进、行宽和换行符
python lua_format_tool.py scan D:\Projects\Scripts --config stylua.toml --indent-width 2 --column-width 100 --line-endings Windows

# 格式化预览，不写文件
python lua_format_tool.py format D:\Projects\Scripts --config stylua.toml --diff

# 实际格式化，默认创建备份
python lua_format_tool.py format D:\Projects\Scripts --config stylua.toml --write

# 包括 Luau 文件，使用 Luau 语法
python lua_format_tool.py scan D:\Projects\Scripts --syntax Luau --include-luau

# 指定格式化器和中文源文件编码
python lua_format_tool.py scan D:\Projects\Scripts --stylua C:\Tools\stylua.exe --encoding gb18030
```

退出码：`0` 全部符合规则或请求的格式化成功；`1` 存在格式问题且尚未写入；`2` 存在解析、读取、写入或参数错误；`130` 用户中断。扫描没有匹配文件时输出数量 0，并退出 0。

JSON 里的 `files` 和 `counts_before_format` 记录格式化前的扫描结果；`applied` 记录成功写入的文件，`write_errors` 记录写入失败。使用 `--diff` 时报告包含修改差异。

## 文件处理

- 默认递归扫描 `.lua`，扩展名不区分大小写；`.luau` 需要启用选项。
- 默认排除 `.git`、`.svn`、`.venv`、`node_modules`、`__pycache__`，可以添加目录名或相对路径通配符，例如 `vendor`、`Generated/*`、`**/*.d.lua`。排除规则区分大小写，不支持 `.gitignore` 的 `!` 反向规则。
- 不跟随符号链接或 Windows 目录联接，避免扫描到目录外和递归循环。
- 自动编码先识别 UTF-8/UTF-16 BOM，再尝试 UTF-8、GB18030。写入保留识别到的编码和 BOM；无 BOM 的 UTF-16 不自动识别。无法正确解码时可手动选择编码。
- 检查由 StyLua 解析完成，并开启 `--verify` 验证格式化结果。Lua 语法错误会显示解析器原始原因，且不能自动格式化。该工具只检查格式和可解析性，不检查未定义变量或玩法逻辑。
- 原因说明基于真实格式化差异：缩进、行尾空白、换行符、末尾换行、引号和布局。引号/空白分类是差异推断，精确修改以预览为准；不会把格式化器保留的长字符串内容当作独立规则修复。
- 默认备份为 `文件名.lua.luaformat.bak`。已有不同内容的备份时拒绝覆盖；需要再次格式化时先移走旧备份，或主动关闭备份选项 / 使用 `--no-backup`。
- 写入前再次校验源文件与扫描快照一致，以同目录临时文件替换原文件；只读文件或扫描后发生修改的文件显示失败原因。批量处理中某个文件失败会继续处理其他文件，已经写入的文件保留。

## 验证

```powershell
python install_stylua.py
python -m unittest discover -s tests -v
```

测试涵盖真实 StyLua 格式化、中文编码与 BOM、Lua/Luau 语法、长字符串/注释、规则导入、扫描排除、备份和修改冲突，以及 GUI 后台扫描、详情、格式化和报告。没有安装 StyLua 时，集成测试会明确跳过；没有显示环境时，GUI 测试会明确跳过。
