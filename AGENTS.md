# AGENTS.md

本文件是 LocalNetFTP 项目的 AI 协作说明。修改本文件时必须使用 UTF-8 读写。

## 项目目标

做一个 Windows 局域网文件共享工具，使用 Python 开发并打包为 exe。程序常驻系统托盘，点击托盘图标显示浮窗。用户可以把文件或文件夹拖到浮窗中，选择局域网内一个或多个在线用户后直接传输。

核心体验要求：
- 默认接收目录为对方 Windows `Downloads` 文件夹，设置中可以修改。
- 设置中可以选择是否开机自启动。
- 支持文件和文件夹传输。
- 支持断点续传。
- 支持多线程传输。
- 支持多台电脑同时在线时的用户发现、选择和多目标发送。
- 界面以轻量、稳定、少打扰为主，不做复杂营销页或无关装饰。

## 技术方向

- 语言：Python 3.10+。
- 平台：Windows 优先。
- GUI/托盘：优先使用成熟库，例如 PySide6/Qt；如改用其他方案，需要说明原因。
- 打包：使用 Nuitka，产物为 Windows exe。
- 配置：用户配置应保存到 Windows 用户目录下的应用配置位置，不要写死到项目目录。
- 网络：局域网发现、身份标识、传输协议、断点续传元数据必须有清晰边界；不要把协议细节散落在 UI 代码中。
- 文件传输：路径处理必须使用 `pathlib`；文件名冲突、目录遍历、防止覆盖策略需要显式处理。

## 目录约定

项目初期建议结构：
- `src/localnetftp/`：应用源码。
- `src/localnetftp/ui/`：托盘、浮窗、设置界面。
- `src/localnetftp/network/`：局域网发现、连接管理、传输协议。
- `src/localnetftp/transfer/`：文件扫描、分片、断点续传、多线程传输。
- `src/localnetftp/config/`：配置读写、开机自启动设置。
- `tests/`：自动化测试。
- `scripts/`：开发、打包、发布辅助脚本。
- `.github/workflows/`：CI 与 exe 打包流程。

如果实际代码已经形成其他结构，优先跟随现有结构，并保持职责清晰。

## 开发规则

- 不明确的需求先向用户确认，不做大范围猜测。
- 每次修改尽量保持小步提交，避免一次提交混入多个无关目标。
- 修改代码后必须说明做了什么、验证了什么、下一步建议做什么。
- 不要提交密钥、证书、真实设备标识、用户目录或本机隐私信息。
- 不要引入新的重量级依赖，除非能明显降低复杂度或提升稳定性，并在提交说明中解释。
- 不要为了演示而伪造真实传输能力；未实现的功能必须明确标注为 TODO 或禁用入口。
- Windows 相关功能要考虑普通用户权限，不默认要求管理员权限。

## Git 和打包流程

- 本项目应使用 Git 管理。
- 每次完成一次明确修改后生成一个 Git commit；如果当前目录还不是 Git 仓库，先询问用户是否初始化。
- 不要自动 `git push`，除非用户明确要求。
- 不要每次 commit 后自动打包 exe。只有用户明确要求打包、发布或验证 exe 时，才执行 Nuitka 打包。
- 手动打包入口：
  - `scripts/build_exe.bat`
  - 或 `python scripts/build_exe.py`
- 每次完成代码或文档修改后必须同时启动 Python 正式版和 Python debug 版做冒烟验证：
  - 正式版：启动 `python -m localnetftp`，等待数秒确认进程仍在运行，然后关闭该进程。
  - debug 版：启动 `python -m localnetftp --dev-instance DEBUG`，等待数秒确认进程仍在运行，然后关闭该进程。
  - 如需手动 debug，可额外运行 `python scripts/start_debug_client.py DEBUG`。
- 每次本地验证完成后，最终回复必须明确说明测试结果、commit hash、Python 正式版启动验证、Python debug 版启动验证和 git 状态。若本轮执行了打包，再额外说明 exe 路径和 exe 冒烟结果。
- 打包产物不要提交进源码仓库，除非用户明确要求发布二进制文件。

推荐后续命令占位：
- 安装依赖：`python -m pip install -r requirements-dev.txt`
- 运行测试：`python -m pytest`
- 打包 exe：`scripts/build_exe.bat`
- 启动 debug 客户端：`python scripts/start_debug_client.py DEBUG`
- 启动双开验证：`python scripts/start_dev_pair.py`

命令不存在时，先创建或更新相应脚本，再更新本文件。

## 固定完成标准

每次代码或文档修改完成前，至少执行：
- `python -m pytest`
- `python scripts/verify_local_transfer.py`
- `git status --short`

每次生成 commit 后，额外执行：
- Python 正式版冒烟启动：`python -m localnetftp`
- Python debug 版冒烟启动：`python -m localnetftp --dev-instance DEBUG`

不要在每次 commit 后自动执行 Nuitka 打包。需要打包时手动运行：
- `scripts/build_exe.bat`
- 或 `python scripts/build_exe.py`

涉及 iroh、公网 ticket、Nuitka 打包或 DLL 加载时，还必须做一次 iroh ticket 本机烟测：生成 ticket、接收 ticket、确认文件保存成功。若因为网络环境限制无法验证公网链路，必须在最终回复中明确说明限制和已完成的本机验证范围。

## 测试和验收

重点测试范围：
- 配置读写和默认下载目录。
- Windows 开机自启动开关。
- 文件/文件夹扫描和相对路径保留。
- 分片传输、校验、断点续传。
- 多线程传输时的进度、取消、失败重试。
- 多目标发送时的部分成功、部分失败处理。
- 局域网发现和离线用户清理。
- 托盘、浮窗拖拽、设置界面的基本交互。

完成传输相关改动时，至少补充单元测试或可重复的本地验证步骤。涉及真实网络、托盘或 Windows 自启动的功能，如果自动化测试困难，必须给出手动验证清单。

## 文档维护

- 当新增脚本、命令、目录结构、打包方式或关键架构决策时，同步更新本文件。
- 长篇设计文档应放到 `docs/`，这里只保留高频、稳定、对 AI 协作有直接帮助的规则。
- `AGENTS.md` 应保持精炼、具体、可执行；不要放入密钥、长日志、生成内容或临时聊天记录。

## 参考原则

整理自 AGENTS.md/Codex 常见最佳实践：
- 根目录放项目级说明，必要时在子目录放更具体的 `AGENTS.md`。
- 写清楚构建、测试、目录、风格、安全边界和完成标准。
- 使用具体命令和具体限制，少写空泛偏好。
- 保持内容短而常用，罕见细节放到普通文档中。
