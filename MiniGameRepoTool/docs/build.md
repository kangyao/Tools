# 编译构建

从主窗口点击“编译构建”。窗口使用当前方案的根目录，生成任务和编译任务随方案保存、复制、导入、导出。高级设置默认折叠。

## 生成任务

“添加 BAT”扫描 `<工程根目录>/SetupScript` 中名称含 Generate 的 BAT，可一次选择多个。当前 C:/MiniGame 的入口包括 MiniGame、AICore、CoreAIHub、MiniGameServer、RainbowEditor 和通用生成 BAT。

每行包含：启用、BAT、参数、输出目录、状态。“复制任务”可以为同一 BAT 添加多条任务，任务有独立 ID、参数和输出目录。

例如，对 `Win_Product_GenerateProject.bat` 添加两行：

| 参数 | 输出目录 |
| --- | --- |
| `--product MiniGame` | `Projects/custom-game` |
| `--product AICore` | `Projects/custom-ai` |

参数遵循 Windows 引号规则，例如 `--configuration Profile --binary-dir "C:\Build Output\Game"`。输出目录支持工程根目录下的相对路径或绝对路径；该列由工具转换为 `--build-dir`，不要在参数列重复填写。同一 BAT 的重复任务应使用不同参数或独立输出目录。

工具在工程根目录执行原 BAT，设置 `CI=1` 并附加 `--no-pause`。BAT 退出码非零、取消或超时均视为失败，已有旧 SLN 不会覆盖失败结果。BAT 路径必须位于当前工程的 SetupScript 中；参数是普通参数列表，不支持 CMD 命令拼接和环境变量展开。

成功后优先读取控制台返回的元数据与已发布的 `MINIW_PRODUCT.json`，提取实际 SLN、主目标、配置和平台。支持一个 BAT 返回一个或多个 SLN。自定义 BAT 不返回 JSON 时，填写输出目录，工具只在该目录中收集元数据或 SLN，不扫描整个 Projects 猜测结果。

本次生成的 SLN 自动加入编译表。再次生成相同任务会更新关联行，并保留已有 ID、勾选状态、目标、配置、平台和动作；不会重复追加同一任务的结果。即使后续任务失败，已经生成的结果仍会保存。

## 多 SLN 编译

“添加 SLN”支持一次选择多个已有解决方案；“读取目标”从所选行对应的 SLN 读取 C++ 项目，排除解决方案文件夹。

每行独立设置：启用、SLN、编译目标、配置、平台、Build / Rebuild / Clean。允许对同一 SLN 添加不同配置或不同目标的任务。导入时优先使用元数据中的主目标和配置，否则从 SLN 中读取。

编译按勾选的行顺序执行，每次只运行一个 IB 会话。调用 `mini-compile-ib/scripts/invoke-mini-ib-build.ps1` 的 `-SolutionPath` 参数，不再根据 VS 版本拼出固定的 MiniGame.sln。所选技能目录需要包含支持该参数的新版脚本。每个任务先 ValidateOnly，再启动 BuildConsole；检查 SLN、实际 C++ 目标和配置。缺少 SLN 时提示先生成，不自动生成替代工程，也不回退到直接 MSBuild。

## 执行与停止

- 顶部选择“构建设置 / 生成 SLN 工程 / IncrediBuild 编译”，执行顺序固定为设置 → 生成任务 → 编译任务。可以只选择其中一步。
- “保存”只保存任务和参数；“检查”只校验入口与已有 SLN/目标，不执行设置、生成或编译。尚未生成的结果需要先生成才能检查。
- “执行所选步骤”执行所选步骤中已启用的任务，逐行展示状态；失败后不再启动后续任务。
- “停止后续任务”等待当前任务完成；“中断当前任务”终止当前任务并停止后续任务。IB 仅通过本次独立会话文件停止，不按进程名全局停止。
- “日志目录”打开本次运行目录。已完成的生成和编译产物不会回滚。

不同工程目录的同步和构建可以在不同工具窗口中并行执行。同一工程的不同方案，以及相互嵌套的工程目录仍共用互斥保护；占用时提示工程目录，未启动的任务显示“未执行”，释放后可重试。旧版工具运行中的任务仍持有全局锁，需等其结束并重新打开工具后才能按工程并行。

构建设置仍复用 SetupWin.py，支持内置 Python 解压、离线依赖包和打包依赖。同步后的自动准备继续只执行构建设置，并检查环境必需仓库；生成和编译由用户在构建窗口选择。

## 高级设置与日志

高级设置包括构建设置 Python、离线依赖包、依赖打包、生成环境变量、IB 技能目录、BuildConsole 路径、日志目录、监视器、设置/生成/检查超时。编译不限时，可手动中断。生成 BAT 的 VS、架构、产品等参数填写在各任务的参数列，由原 BAT 解析。

参数通过 JSON、PowerShell 参数表和带引号的 BAT 环境参数传递，支持中文和空格路径。CMD 特殊字符 `% ! ^ & | < >`、参数内部引号和控制字符会被拒绝。

每次运行保留：

~~~text
builds/<run-id>/
  build.log
  generate-01-<task-id>/generate.bat
  compile-01-<task-id>/
    invoke-mini-ib-build.ps1.request.json
    incredibuild.log
    incredibuild.session.json
  compile-02-<task-id>/...
~~~

IB 结果同时核对启动器退出码和本次会话中的 BuildConsole ExitCode。原始日志完整保留，警告不会自动改为失败。

旧版单工程配置读取时会迁移为一条生成任务和一条关联的编译任务，保留旧的目标、配置、平台和动作；JSON schema_version 仍为 1。
