# Execution ledger — docs/implementation-plan.md

- 用户已授权实现；设计与目录已确认。所有产品文件位于 D:\git\Tools\MiniGameRepoTool。
- 已创建开发分支 codex/minigame-repo-tool。未进行远端推送。
- Pre-flight: UI/Worker 消费统一 Profile、Snapshot、RepoResult 与 RepoService 事件；核心不依赖 Qt。
- Task 1: done。配置、路径与依赖验证、Git 状态和执行、进程中断、运行记录已实现。
- Task 2: done。Qt 主窗口、方案与仓库编辑、后台线程、日志、重试和环境准备入口已实现。
- Task 3: done。安装、双击启动、实际窗口截图、示例配置、使用文档、最终完整回归和独立复核已完成。

## 已修复并验证的边界

- 单分支克隆后切换其他分支时，补充该分支的 fetch 映射，再设置跟踪分支。
- 同步子仓库不更新已经可用但未选中的父仓库，即使父分支落后于缓存远端。
- 同时检查远端目标与本地目标分支的目录树，阻止父提交覆盖子仓库或其祖先目录；遵循 Windows 大小写规则。
- switch 与 merge 使用 --no-overwrite-ignore，保留被忽略的本地文件。
- 未初始化子模块在克隆前检查父 index，包括目录不存在、目录为空、路径大小写变体和子模块后代路径。
- 运行 JSON、日志和 UI 对 URL 凭据脱敏；禁止在配置保存 HTTP 用户信息形式的令牌。
- 分支或 origin 配置在副本上验证、保存成功后才替换，失效旧状态和结果。
- 核对真实 Win_Setup.bat 的 pushd ..，直接执行 SetupWin.py 时使用工程根目录并保留真实退出码。

## 验证记录

- 初始版本完整测试：45 passed，126.79 秒，0 失败。命令：.venv\Scripts\python.exe -m pytest -q --tb=short --junitxml=artifacts\test-results.xml。
- 配置与 UI 回归先复现失败，再修复通过；Git 场景在系统临时目录创建真实仓库。
- 第一轮完整测试：38 passed，117.31 秒。
- 扩展 Windows 路径用例时发现测试本身误要求缺失子模块的父仓库状态为空；已改为检查操作前后状态不变，保留既有 D module。
- 独立审查：5 组问题已全部关闭；最终复核 10 个目录保护和子模块用例通过，36.90 秒。
- Install.bat：成功创建/使用项目虚拟环境，editable 安装成功。
- pip check：No broken requirements found。
- compileall：成功。
- run.py --smoke-test：实际 Qt 窗口打开并退出，截图成功。
- Start.bat --smoke-test：验证双击启动同一路径，窗口截图成功。

## 验证边界

没有对真实 E:\MiniGame 执行克隆、fetch、切分支、同步或环境准备。真实工程仅用于只读核对批处理和 SetupWin.py 的行为。认证错误有分类代码，真实 SSH/凭据服务未做端到端验收。

当前交付为 Python 源码与虚拟环境，无独立 exe。运行锁覆盖同一配置目录的实例；不同配置目录和其他 Git 客户端不受该锁控制。

## 文件列表、Diff 与 Discard 扩展（2026-09-29）

- 用户要求能看到修改的文件并操作 Discard；新增独立文件对话框，从仓库详情按钮或双击仓库进入。
- 按文件展示暂存/未暂存状态，分别预览两个 Diff，支持未跟踪文本/二进制预览、勾选文件、确认后备份并丢弃、进入已配置子仓库。
- Discard 只处理所选文件；工作区按原始字节备份，暂存区以完整 binary patch 备份；固定 HEAD、literal/NUL 路径、前后指纹检查、子模块和链接保护、部分失败记录已实现。
- 完整回归：70 passed，189.21 秒，0 失败，包含 45 项原有用例及当时的 25 项新增用例。结果：artifacts/test-results.xml。
- 完整回归之后，针对“窗口在首个扫描定时器触发前关闭”补充 1 项回归，先确认失败，再禁止关闭后启动 worker；文件对话框全部 3 项 UI 用例重新通过，17.82 秒。结果：artifacts/changes-ui-results.xml。目前共 71 项用例。
- 核心覆盖：HEAD/index/worktree 各自变化、选择性丢弃、rename/新增/删除、未跟踪文件、literal 路径、完整二进制暂存补丁超过 4 MB 后重新应用还原、备份/清单/补丁写失败、部分失败、子模块指针、嵌套仓库、符号链接模式、intent-to-add、merge 与 stash 冲突。
- 独立审查发现并关闭两项边界：重命名原路径重建且被忽略时仍应禁止覆盖；stash apply 冲突没有 MERGE_HEAD 时仍应禁止全仓库 Discard。对应回归先失败后修复通过，最终复核无遗留关键问题。
- pip check、compileall、git diff --check 通过。run.py --smoke-test 实际窗口正常启动退出，新增主窗口按钮可见。
- 真实 E:\MiniGame 只读验收：CommonResource 列出 29 项（子模块内部文件），Editor 列出 CoreAIHub/App/HubApplication.h；实际 Windows Qt 窗口的中文、文件列表和 Diff 已截图核对。截图：artifacts/changes-CommonResource.png、artifacts/changes-Editor.png。
- 没有对真实工程执行 Discard、切分支或同步。所有破坏性和恢复验证均在系统临时 Git 仓库进行。
- README、使用说明、设计文档同步更新；明确同时丢弃所选文件的暂存/未暂存改动、未跟踪文件删除、备份恢复步骤、子模块提交指针保留及不支持按 Diff 行丢弃。

## 并行状态检查与 Windows 原生菜单（2026-09-30）

- 用户指定增加“并行读取状态”和“Windows 原生右键菜单”。状态检查采用最多 8 个线程，每项独立 runner 和日志回调，逐项更新快照与计数进度；远端检查对共享 git-common-dir 的 worktree 互斥。
- 停止队列不再派发新检查，已开始的检查完成；中断通过共享事件终止本批次全部活动 Git/SSH 进程树。同步和 Discard 保持原有执行流程。
- 仓库行右键调用真实 Windows Shell 文件夹背景菜单，支持本机安装的扩展；使用右键所在行并保留勾选。菜单期间持有运行锁并禁用其他任务入口，取消不扫描，调用命令后刷新本地状态。
- 原生菜单适配复用用户提供的 Git Fleet ctypes 实现，作为本项目内的独立模块维护，不依赖 Fleet 文件位置，也不新增 pywin32。
- 先用失败用例复现原有串行检查和缺少原生右键入口，再实现通过。首轮专项 10 passed，6.83 秒；首轮完整回归 81 passed，162.71 秒。
- 独立审查发现“原生菜单期间关闭窗口，菜单返回后仍启动扫描线程”的 P2 边界。新增回归先失败，再延迟关闭直到菜单释放资源，并阻止关闭后的任务入口；执行菜单和取消菜单两种返回均覆盖。只读复核确认该项关闭，无新的必要修复项。
- 最终完整回归：84 passed，167.19 秒，0 失败。命令：.venv\Scripts\python.exe -m pytest -q --tb=short --junitxml=artifacts\parallel-native-test-results.xml。包含原有 71 项、并行检查 7 项、原生菜单 UI 5 项、Windows Shell 集成 1 项。
- 并行回归覆盖 8 路上限、独立 runner 与日志归属、逐项结果、停止后续派发、同时中断两个真实子进程、单仓库失败隔离，以及真实 linked worktree 的远端检查互斥。
- 菜单验证覆盖中文路径、右键行与勾选范围、命令后刷新、缺失目录与空白处、菜单期间关闭窗口。Windows 集成测试使用真实 COM 接口查询临时中文目录的 Shell 菜单，获得菜单项但不显示菜单、不调用命令。菜单 UI 与 Shell 专项最终 6 passed，2.81 秒。
- pip check、compileall 和 git diff --check 通过。README、使用说明、设计文档同步更新，说明外部程序可能在菜单返回后继续工作，需在其完成后重新检查；外部菜单操作不经过工具 Discard 备份。
- 界面自动化验收中，用户按 Esc 停止了 Computer Use，后续未继续操作桌面；完整的交互弹出、位置和扩展命令执行验收未完成，不能用上述接口测试替代该验收。未执行任何外部菜单 Git 命令。
- 本次没有对真实 E:\MiniGame 执行 fetch、切分支、同步或 Discard；所有会修改文件的回归使用临时测试仓库。

## 按仓库忽略修改提醒与不备份强制更新（2026-09-30）

- 用户要求按仓库增加忽略修改提醒和强制更新选项，明确强制更新不备份，并要求直接实现、不使用 brainstorming。两个选项放在方案表的仓库名称右侧，默认关闭，可随方案保存、复制、导入和导出。
- 忽略修改提醒仅调整主列表文字、颜色和详情展示，不改变 Git 状态或文件/Diff 服务；其他错误仍显示。仅忽略提醒不会绕过普通同步的工作区检查。
- 强制更新在明确选择该仓库并点击同步后生效，丢弃暂存、未暂存及普通未跟踪文件，不创建文件备份、stash、补丁或恢复分支。保留本地提交，分叉仍阻止；保存配置和检查状态只生成计划，不丢弃文件。手动 Discard 的备份流程保持不变。
- 清理复用现有路径、快照、指纹和子仓库识别。先核对远端、目录布局、其他 worktree、未完成操作、ignored 恢复路径，再按固定 HEAD 和 NUL literal pathspec 恢复普通文件；仅删除逐个核对的普通未跟踪文件，不递归删除目录。
- 配置/界面先观察到 2 项失败，再实现通过；强制更新先观察到 6 failed / 5 passed（147.39 秒），再实现首轮专项 15 passed（94.76 秒）。补充子模块、未配置嵌套仓库和 stash apply 索引冲突保护后，首轮完整回归 102 passed（434.01 秒）。
- 独立审查复现并关闭两类 P1：自动依赖在两次检查之间变脏，以及 included/重试扩大明确选择范围；暂存删除路径上的 ignored replacement 被恢复覆盖。分别新增真实 Git 回归先确认失败，再在实际丢弃入口核对原始选择、限制自动勾选及重试，并在丢弃前分批检查被忽略路径。只读复核确认两类问题均已关闭，无新增阻止完成问题。
- 两项后端修复回归先通过；两仓库 UI 重试回归在多组 Git 测试同时运行时超过原 30 秒等待限制，将该专项用例等待上限调整为 120 秒，验证条件保持不变。
- 最终完整回归：105 passed，265.79 秒，0 失败，包含原有 84 项及本次 16 项强制更新、5 项配置/界面用例。命令：.venv\Scripts\python.exe -m pytest -q --tb=short --junitxml=artifacts\force-update-final-results.xml。此前超时的 UI 重试回归及三条复核问题回归均通过。
- pip check、compileall、git diff --check 通过。离屏渲染方案对话框并检查两列复选框、中文说明和位置；预览使用示例数据，截图位于 artifacts/repo-options.png，没有操作用户桌面或保存用户实际方案。
- README、使用说明和设计文档同步更新；本次不对实际工程执行强制更新，所有文件丢弃验证仅在临时真实 Git 仓库进行。

## 取消前置仓库等待，所选仓库独立同步（2026-09-30）

- 用户截图中 Source 有本地修改，导致其下 RainbowEngine 虽可快进仍被标为待处理；用户要求不要等待前置仓库。根因为同步器自动扩展父仓库，并用父仓库的失败或待处理结果阻止后代。
- 移除父仓库自动加入、同步就绪检查和后代阻塞规则。同步只处理明确选择的仓库，按配置列表逐项执行；父仓库缺失、脏工作区或执行失败均不阻止选中的子仓库。单选子仓库克隆时只创建必要父目录，不克隆父 Git 仓库。
- 执行计划显示独立同步，删除自动勾选事件；旧 depends_on 字段继续用于列表展示，界面名称改为“排序参考”。目录树和路径保护保留，检查仍最多 8 路并行。强制更新仍核对明确选择，重试不扩大选择到父仓库。
- 保留各仓库自身的目录布局、子模块、工作区、远端和分叉检查；环境准备仍要求全部必需仓库就绪，不阻止已选择仓库的 Git 同步。
- 先运行 3 项回归确认旧实现均失败（39.26 秒），再验证 7 项相关回归全部通过（137.28 秒），覆盖缺失父仓库、父失败、显式 depends_on、父脏子快进、未选择的强制更新父仓库，以及界面重试与勾选范围。
- 最终完整回归：108 passed，223.73 秒，0 失败。命令：.venv\Scripts\python.exe -m pytest -q --tb=short --junitxml=artifacts\independent-sync-final-results.xml。
- pip check、compileall 和 git diff --check 通过；复核确认同步队列不再扩展选择，目录层级只参与展示与路径保护，强制丢弃入口保留明确选择检查。
- README、使用说明与设计文档同步更新；早期 HTML 原型和实现计划标为历史规则。本次未操作用户桌面或实际 E:\MiniGame，Git 写操作均使用临时测试仓库。

## 统一编译构建与参数配置（2026-09-30）

- 用户指定 Win_Setup.bat 为构建设置、VS2019_64-MiniGame.bat 为 SLN 生成入口，并指定 W:/git/skills/mini-compile-ib/SKILL.md 的 IB 方法。读取这两个 BAT、底层 Python 入口及技能的 invoke/read/stop 脚本后，实现统一“编译构建”窗口。
- 三个步骤可单选或顺序执行，默认仅 MiniGame / VS2019 / Debug / x64 的 IB 编译。BuildOptions 随方案保存、复制、导入导出，旧配置自动补默认值；管理仓库时保留构建参数，单独保存构建参数不清空 Git 状态。
- 构建设置复用 Python39 解压与 SetupWin.py，增加离线依赖包和依赖打包参数。生成使用原脚本的 dev 模式、工作目录和环境变量；支持原 BAT 的 mComplieType 与开关参数，USE_RAINBOW_LIB=1 时先执行 PullEngine.py，跳过 pause。
- 原生成脚本未传播 os.system 失败；新增 Python 启动器保留原脚本，捕获内部命令非零退出并阻止后续编译，避免旧 SLN 掩盖生成失败。临时工程回归确认失败码 7 与旧 SLN 内容保留。
- IB 通过 JSON 参数表调用指定技能脚本；先 ValidateOnly 再执行，按独立会话 ExitCode 判定结果，并读取原始日志摘要。中断/超时使用本次会话文件调用停止脚本，再终止调用进程；不传 AllowConcurrent，不回退 MSBuild。
- 每次构建保存独立 build.log、IB 日志/会话/请求参数以及 runs 中的步骤结果；共用原有运行锁。手动构建不要求 Git 仓库干净或同步，原自动环境准备仍只执行设置并保留必需仓库检查。
- 首轮专项回归 33 passed / 2 failed（38.03 秒），发现 QDialog.finished 与工作线程结束处理函数重名。改为 job_finished 后，4 项界面回归通过（1.08 秒）。使用离屏 Qt 渲染三个参数页并检查中文、布局与按钮，无实际工程执行。
- 最终完整回归：135 passed，252.33 秒，0 失败，包含原有 108 项和本次 27 项构建/配置/界面/进程回归。命令：.venv\Scripts\python.exe -m pytest -q --tb=short --junitxml=artifacts\build-final-results.xml。
- pip check、compileall、示例配置解析和 git diff --check 通过。构建页面截图位于 artifacts/build-compile.png、build-generate.png、build-setup.png。
- 对真实 E:/MiniGame 只做 ValidateOnly：设置入口与生成预设通过；缺少 Projects/vs2019-win64-MiniGame/MiniGame.sln，编译检查明确提示先生成。测试还调用真实 IB 技能的 ValidateOnly 校验临时 SLN 与不可执行的占位 BuildConsole，未启动实际 IB。
- README、使用说明、设计、示例配置和 docs/build.md 同步更新。所有设置、生成与编译执行回归使用临时工程及替身脚本；本次没有对实际 MiniGame 执行构建设置、工程生成或编译。

## 初始化加速第一版（2026-09-30）

- 按 docs/initialization-acceleration-plan.md 实现第一版：InitOptions 与仓库级 init_source 随方案保存、复制、导入导出；旧配置读取为纯网络下载，默认模板与新建方案为自动复用并预填本机存在的 D 盘工程。
- 新增 acceleration.py：SourceScanner 只读探测来源（真实工作树根、远端身份、common dir、lfs.storage、浅克隆/部分克隆/gc.pid）；Initializer 在目标同级临时目录中执行 no-checkout + reference-if-able + dissociate 克隆，按目标提交复制并 SHA-256 校验 LFS 对象，补齐下载、检出、验证后同盘改名就位。service.py 的克隆分支接入，失败按配置回退一次网络克隆或停止。
- 界面：主窗口“初始化计划”、管理方案“初始化加速”区域、“扫描本地工程”和仓库表“初始化来源”列；执行计划页展示来源、排除原因和执行方式，日志输出六个阶段，RepoResult.details 写入运行记录。
- 调试中发现并处理：取消时扫描失败被误判为“无来源”（改为先判断中断）；git-lfs 退出码 2 被归类为“分支不存在”（LFS 阶段改用原始输出）；git-lfs 本地文件传输会硬链接对象，测试改为替换文件而非原地改写；本机 Git 2.45.1 在带 LFS hook 的 clone 检出阶段报错，测试源仓库改为先无检出克隆再检出，加速流程本身不受影响。
- 新增 25 项回归（test_acceleration.py 23 项、test_acceleration_ui.py 2 项），覆盖远端身份、配置兼容与校验、跨分支复用并解除借用（删除源后 fsck 通过）、源工作区修改不进入目标、父仓库误识别/浅克隆/远端不同/目录缺失、停止策略、指定源工程不改用其他工程、其他相对路径按身份发现、分支不存在不回退并清理临时目录、就位前目标被占用不覆盖、中断、父失败子仍初始化、LFS 无需下载的本地复用、损坏对象拒绝后下载且源缓存不变、浅克隆仅供 LFS，以及对话框与主窗口流程。
- 最终完整回归：160 passed，321.95 秒，0 失败。命令：.venv\Scripts\python.exe -m pytest -q。布局微调后复跑界面相关 11 项通过。compileall、git diff --check、示例配置解析通过。管理方案对话框离屏截图：artifacts/init-acceleration-dialog.png。
- 真实环境只读核对：以默认方案和草稿区不存在的根目录执行初始化计划，只对 D:\MiniGame、D:\AIMiniGame、D:\MiniGameProfiler 运行 rev-parse / config 读取，结果见方案文档“第一版实现情况”。没有对真实工程执行克隆、fetch、复制或配置修改；磁盘不足与 UGit 场景未测试。

## 集成裁定

遵照用户“操作完 git commit，不要 push”的约定，仅将本工具目录提交到 codex/minigame-repo-tool，保留现有目录和分支。无合并、远端推送或工作树清理操作。
