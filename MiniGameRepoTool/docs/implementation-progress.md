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

- 最终完整测试：45 passed，126.79 秒，0 失败。命令：.venv\Scripts\python.exe -m pytest -q --tb=short --junitxml=artifacts\test-results.xml。
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

## 集成裁定

遵照用户“操作完 git commit，不要 push”的约定，仅将本工具目录提交到 codex/minigame-repo-tool，保留现有目录和分支。无合并、远端推送或工作树清理操作。
