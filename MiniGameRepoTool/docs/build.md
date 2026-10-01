# 编译构建

入口：主窗口右下角“编译构建”。工程根目录来自当前方案；所有构建参数保存在该方案的 build 字段中。原配置不含 build 字段时自动使用默认值，不需要迁移文件。

## 常见操作

| 需要做什么 | 勾选步骤 | 常用参数 |
|---|---|---|
| 首次设置并生成、编译 | 构建设置 + 生成 SLN 工程 + IncrediBuild 编译 | VS2019、x64、MiniGame、Debug、Build |
| 日常增量编译 | 仅 IncrediBuild 编译 | 已有工程的目标、配置和平台，动作 Build |
| 重新编译目标 | 仅 IncrediBuild 编译 | 动作 Rebuild |
| 重新生成 SLN | 仅生成 SLN 工程 | VS/平台、生成参数、环境变量 |
| 配置或修复构建依赖 | 仅构建设置 | Python、离线依赖包或依赖打包选项 |
| 清理目标产物 | 仅 IncrediBuild 编译 | 动作 Clean，由 IB 清理该目标 |

默认只选择编译；勾选多个步骤时固定按设置 → 生成 → 编译运行，前一步不成功则后续步骤显示“未执行”。Git 仓库列表的勾选范围与构建步骤无关；手动构建不检查 Git 是否已经同步或有本地修改。

“保存参数”只保存配置。“检查配置”检查选中步骤的文件入口，以及已有 SLN、目标和 IB 程序；编译检查使用技能脚本的 ValidateOnly，不启动 BuildConsole。未生成的 SLN 会在检查中报缺失；可以先执行生成步骤，或直接执行包含生成的完整流程，每一步在实际执行时重新校验。

## 参数

| 参数 | 默认值与含义 |
|---|---|
| Visual Studio | 2019；可选 2022，需要工程已有对应生成预设和本机工具链 |
| 平台 | x64；可选 Win32，同样要求对应预设和工具链可用 |
| Python | Tools/buildtools/Python39/python.exe；可以选择其他 Python，默认版本缺失时构建设置会解压 Python39.zip |
| 离线依赖包 | 留空表示按 Tools/Setup/Dependence.json 下载和设置；指定 ZIP 时传入 SetupWin.py 的 --install，并明确 --workdir 为工程根目录 |
| 依赖打包 | 选中后传 --packzip True，生成 WinSetup.zip；不能与离线包同时使用 |
| 生成参数 | 对应原生成 BAT 的第一个参数 mComplieType，可留空 |
| 生成环境参数 | 每行 名称=值；支持 USE_LUA_JIT、USE_RAINBOW_LIB、USE_DEV_BUILD 等原 BAT 参数，默认值见下方 |
| 编译目标 | MiniGameApp；SLN 中的 MiniGame 只是解决方案文件夹，不能作为目标。可以输入其他已有目标，也可点“读取 SLN 目标”选择实际存在的 C++ 项目（当前目标不在列表中时自动切到 MiniGameApp）。编译和检查前，工具先确认目标是 SLN 中的 C++ 项目 |
| 构建配置 | Debug；可选 Release、Profile、EditorDebug、EditorRelease，实际支持情况由现有工程与 IB 判定 |
| 动作 | Build 增量编译、Rebuild 重新编译、Clean 清理，均只针对指定目标 |
| mini-compile-ib 技能目录 | W:/git/skills/mini-compile-ib；必须含 scripts 下的 invoke/read/stop 三个脚本 |
| BuildConsole | 留空由技能在 PATH 和 IncrediBuild 常见安装位置查找，也可指定 BuildConsole.exe |
| 打开 IB 监视器 | 默认关闭；打开后传入技能 OpenMonitor，并生成本次监视器文件 |
| 构建日志目录 | 留空使用工具配置目录/builds；可填写绝对目录或相对工程根目录的路径，每次执行创建独立子目录 |
| 单个命令超时 | 0 不限时；1–1440 分钟，超时使用与中断相同的当前任务停止流程 |

生成默认环境：

```text
USE_LUA_JIT=1
USE_RAINBOW_LIB=0
USE_SANDBOXENGINE_DRIVER_LIB=0
USE_DEV_BUILD=1
USE_UNIVERSE_BUILD=0
USE_POCO_BUILD=1
ADVANCE_BETA_CLIENT_BUILD=0
ENABLE_MEMORY_LEAK_CHECK_LOG=1
```

删掉某个默认变量时恢复它的默认值；将其改为 0 表示关闭。USE_RAINBOW_LIB=1 时，先执行 Tools/Setup/PullEngine.py 获取引擎库，成功后才生成工程。PROJECT_ROOT_DIR、PYTHON_EXE、PYTHONHOME、PYTHONPATH 和 mComplieType 由路径和独立参数生成，不能在环境参数区重复覆盖。

## 与工程脚本的对应关系

| 步骤 | 用户提供的入口 | 工具执行方式 |
|---|---|---|
| 构建设置 | SetupScript/Win_Setup.bat | 必要时使用 Tools/7z.exe 解压 Python39.zip，然后在工程根目录用配置的 Python 执行 Tools/Setup/SetupWin.py |
| 生成 SLN | Tools/ProjectBat/VS2019_64-MiniGame.bat | 在 Tools/buildtools 目录执行原 cmake_generate_projects.py，传入由 VS/平台形成的预设和 dev，保留 BAT 的环境变量；跳过 pause |
| IB 编译 | mini-compile-ib/SKILL.md | 通过该技能的 scripts/invoke-mini-ib-build.ps1 校验、构建；结束后调用 read-mini-ib-result.ps1 读取摘要 |

默认解决方案是 Projects/vs2019-win64-MiniGame/MiniGame.sln。VS 和平台变化时，生成预设及 IB 解决方案目录一起变化。工具使用现有项目脚本，不修改 MiniGame 源码、BAT 或技能脚本，也不把工程生成隐藏在“仅编译”中。

生成脚本原本不返回内部 os.system 调用的失败码。工具使用小型 Python 启动器运行原脚本，将内部命令失败传递为生成步骤失败，并核对预期 SLN；避免已有旧 SLN 掩盖本次 CMake 失败。脚本自身的工具链、依赖和路径限制仍由其实际执行结果报告。

IB 参数通过 JSON 文件传给 PowerShell 适配器，再以参数表调用技能脚本，含空格和中文的路径保持为完整参数。适配器不拼接用户输入为 PowerShell 命令。IB 结果以独立会话 JSON 中的 BuildConsole ExitCode 为准，保留原始日志；编译警告不会自动变为失败。缺少 IB、缺少目标或已有活动会话时，报告技能给出的原因，不回退到 MSBuild，不启用 AllowConcurrent。

## 日志、中断和再次执行

构建窗口持续显示输出和每步结果，界面保留最近 8000 行。点击“打开日志目录”可查看本次完整文件：

```text
builds/<本次运行标识>/
  build.log                        设置、生成、IB 与摘要的完整输出
  incredibuild.log                  IB 原始日志（真正开始编译后）
  incredibuild.session.json         IB 退出码和会话信息
  incredibuild.ib_mon               启用监视器时由 IB 生成
  *.request.json                   各技能调用的实际脚本和参数
```

runs 目录还保存本次步骤的结构化结果和工具运行日志。日志目录可按方案修改；每次运行均使用新目录，避免混用历史会话和退出码。

- 停止后续步骤：当前步骤继续完成，余下步骤不运行。
- 中断当前步骤：设置和生成终止当前进程树；IB 使用本次会话文件调用 stop-mini-ib-build.ps1 -IncludeWorkers，并终止本次调用进程。不会发送不带会话信息的全局停止命令。
- 关闭执行中的窗口：确认中断后等待工作线程结束，保留日志。
- 再次执行：处理错误后重新勾选需要的步骤，再点“执行所选步骤”。已完成的设置、工程生成或编译产物不会回滚。

原有“同步后自动准备构建环境”设置继续可用，仅运行构建设置并使用已保存的设置参数；该自动流程仍检查全部“环境必需”仓库。自动流程不执行工程生成或 IB 编译。
