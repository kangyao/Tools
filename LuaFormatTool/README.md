# Lua 格式检查与格式化

Python 桌面工具，选择目录后列出 Lua 文件，点击文件时才检查格式并显示修改差异；未修改的文件复用已检查的预览，再按自定义规则格式化。附带命令行入口，适合批量检查和 CI。

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

1. 选择 Lua 目录或单个文件，或者直接输入路径。左侧自动列出文件，初始状态为 **未检查**，列出文件不调用 StyLua。排除规则使用逗号分隔；路径栏按回车可刷新文件列表。
2. 点击文件，右侧切换到 **修改差异**，后台只检查当前文件。重复点击或切换回未修改的文件时，直接复用检查结果和差异，不重新运行格式化器。文件内容或格式规则改变后，下次查看时才更新；仅时间戳变化而内容相同时仍复用结果。缓存仅保存在当前会话中。**隐藏已通过文件** 会隐藏格式正确的文件，未检查的文件仍然可见。
3. 可切换 **问题原因 / 修改差异 / 原始内容 / 格式化预览**。原因包含行号、规则、原始片段和目标片段。**修改差异** 默认使用左右对比：左侧原始内容、右侧格式化预览，分别显示源文件行号；红色标出修改前 / 删除行，绿色标出修改后 / 新增行，灰色空位保持两边对齐。两侧纵向同步滚动，横向可独立滚动，支持 **上一处 / 下一处** 跳转；也可切换为 **单栏差异**。仅换行符或末尾换行不同时会显示 `[CRLF]`、`[LF]` 或 `[无换行]` 标记（这些标记只用于预览）。
4. 看过差异后，可多选已检查文件并点击 **格式化选中文件**，也可以点击 **格式化已检查问题**。这两个操作只写入已经查看的预览，不会检查或格式化未查看的文件；成功写入后更新缓存和文件状态。文件在预览后被外部修改时拒绝覆盖，需要再次点击更新差异。
5. 点击 **格式配置** 打开独立面板，设置格式规则、源文件编码、是否包括 `.luau` 以及是否备份，点击 **应用** 保存；**取消**、Esc 或关闭面板会放弃未应用的修改。修改格式规则会使旧预览失效，当前显示的文件自动更新，其他文件仍在点击时检查。主界面显示当前规则摘要。
6. 格式配置面板中的 **导入规则 / 导出规则** 使用 TOML，兼容 StyLua 的原有规则；导出的 `[lua_format_tool]` 扩展段记录函数间空行和换行保留选项，需通过本工具使用。导入后点击 **应用** 才生效。**导出已检查报告** 只输出当前列表中已检查且未修改文件的原因和差异，`unchecked` 单独列出未检查或已过期文件，不会触发额外检查。

文件列表加载、当前文件检查和格式化在后台运行，**停止** 会停止后续处理。快速切换文件时优先处理最后点击的文件，较早文件的结果不会覆盖当前预览。

工具记住上次路径和规则，保存在本目录的 `settings.local.json`；此文件和下载的 StyLua 不纳入 Git。

## 可设置的格式规则

| 规则 | 工具默认值 | 作用 |
| --- | --- | --- |
| Lua 版本 | Lua54 | Lua51 / Lua52 / Lua53 / Lua54 / LuaJIT / Luau / CfxLua / All |
| 缩进类型与宽度 | Spaces / 4 | 空格或 Tab；Tab 宽度用于布局估算 |
| 参考行宽 | 120 | 换行参考宽度，长字符串或注释可能超过此值 |
| 换行符 | Unix | LF；Windows 使用 CRLF；不处理时保留源行的 LF / CRLF / CR |
| 文件末尾换行 | Always | 补齐末尾换行；不处理时保留原文件末尾有无换行的状态 |
| 字符串引号 | AutoPreferDouble | 优先双引号、优先单引号、强制双引号或强制单引号 |
| 调用括号 | Always | 单字符串/表参数调用的括号风格；不处理对应 Input，保留原括号 |
| 合并简单语句 | GuardReturn | 仅短条件提前返回：只将不带返回值的 `if ... then return end` 合为一行；不处理对应 Input |
| 函数名后空格 | Never | 定义、调用或两者添加空格 |
| 块首尾空行 | Never | 不处理对应 Preserve，保留块首尾原有空行 |
| 函数之间空行 | Always | 相邻函数之间至少一行空行；不处理对应 Preserve，不额外强制补空行 |

**函数之间空行** 默认启用，覆盖普通函数、局部函数、方法及 `local f = function(...)` / `Module.f = function(...)` 形式的定义，也支持嵌套函数。下一函数的说明注释和 `---@param` 等注解保持与函数相连，空行补在注释之前；已有空行不会重复添加。字符串、长注释和 `stylua: ignore` 区域不参与补空行。此规则与“块首尾空行”独立：前者分隔相邻函数，后者控制函数或条件块内部首尾的空行。

**仅短条件提前返回（无返回值）** 默认启用。只有无内部空行、无注释、无 `else` / `elseif` 分支，并且整行（含缩进）不超过参考行宽的 `if ... then return end` 才会合并。`return value`、函数体、普通执行语句和过长条件块保持多行；字符串、注释与 `stylua: ignore` 区域保持原样。其他布局仍由 StyLua 按参考行宽处理。可在下拉框中显式选择原有的“不处理”或其他合并模式。

```lua
if not target then return end

if not target then
    return "missing target"
end

if target then
    target:Update()
end
```

可在 **格式配置** 的下拉框中选择 **不处理**。需要保留哪些样式就单独选择。换行符和文件末尾换行是独立选项：两个都选“不处理”才会同时保留换行符种类和末尾有无换行。混合换行文件按源行对齐保留，自动折行等布局变化产生的新行沿用对应或相邻源行的换行符；无可参考的换行符时使用 LF。简单语句选择 Input 后，过长的单行语句仍可能因参考行宽而折行。

缩进、字符串引号、函数名后空格和参考行宽没有 StyLua 提供的独立停用模式，仍按选定规则格式化。需要完整保留某段代码时，可以在代码中使用官方的 `-- stylua: ignore`，或 `-- stylua: ignore start` / `-- stylua: ignore end`。

配置格式和参数含义参见 [StyLua 2.6.0 官方文档](https://github.com/JohnnyMorganz/StyLua/blob/v2.6.0/README.md#configuration)。导入 TOML 时，未指定的 StyLua 原有字段遵循其默认值（包括 Tab 缩进、All 语法和不合并语句）；新建工具配置采用上表默认值。工具专有的换行保留、函数间距和 GuardReturn 选项保存在 `[lua_format_tool]` 中；缺少该扩展段的旧配置也默认启用函数间空行。GuardReturn 模式先让 StyLua 按 Never 展开语句，再仅合并符合条件的短提前返回，之后补齐函数间空行、恢复对应的换行状态。

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

# 不处理换行符、末尾换行、简单语句合并、调用括号和块首尾空行
python lua_format_tool.py scan D:\Projects\Scripts --line-endings Preserve --final-newline Preserve --collapse-simple-statement Input --call-parentheses Input --block-newline-gaps Preserve

# 不额外强制函数之间留空行（默认 Always，可省略）
python lua_format_tool.py scan D:\Projects\Scripts --function-spacing Preserve

# 只合并短的 if ... then return end（工具默认模式）
python lua_format_tool.py scan D:\Projects\Scripts --collapse-simple-statement GuardReturn

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
- 写入前再次校验源文件与预览快照一致，以同目录临时文件替换原文件；只读文件或预览后发生修改的文件显示失败原因。批量处理中某个文件失败会继续处理其他文件，已经写入的文件保留。

## 验证

```powershell
python install_stylua.py
python -m unittest discover -s tests -v
```

测试涵盖真实 StyLua 格式化、中文编码与 BOM、Lua/Luau 语法、长字符串/注释、混合换行和末尾换行保留、语句/括号/空行的输入样式保留、规则导入导出、扫描排除、备份和修改冲突，以及 GUI 自动加载文件列表、按需检查与缓存、快速切换、详情、格式化和已检查报告。没有安装 StyLua 时，集成测试会明确跳过；没有显示环境时，GUI 测试会明确跳过。
