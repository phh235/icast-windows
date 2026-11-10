# <sub><img src="icast/icon.png" width="36" height="36" alt=""></sub> iCast

[English](README.md) · [Tiếng Việt](README.vi.md) · **简体中文**

在 Windows 上镜像和控制 iPhone。项目由 **phh235** 维护。

iCast 可以让 Windows 10 或 11 电脑接收 AirPlay（隔空播放）。通过 Wi‑Fi 就能在电脑上查看 iPhone 屏幕、播放声音，手机无需安装 App。如果想用鼠标和键盘操作 iPhone，还可以通过 USB 配置 WebDriverAgent。

[安装](#安装) · [让智能助手安装](#让智能助手安装) · [屏幕镜像](#屏幕镜像) · [通过 USB 控制](#通过-usb-控制) · [故障排查](#故障排查)

## 安装

你需要 Windows 10 或 11、Python 3.11 或更高版本，并让 iPhone 和电脑连接到同一 Wi‑Fi 网络。安装 Python 时请勾选 **Add python.exe to PATH**。[下载 Python](https://www.python.org/downloads/)

1. 在 GitHub 上选择 **Code → Download ZIP**，解压到打算长期保留的文件夹，例如 `C:\Apps\icast-windows`。如果使用 Git，可以运行 `git clone https://github.com/phh235/icast-windows.git`。
2. 打开项目文件夹，选择一种安装方式。

   - **镜像和 USB 控制。** 运行 `install.bat`，安装电脑端的全部依赖。手机端的配置见[通过 USB 控制](#通过-usb-控制)。
   - **仅镜像和播放声音。** 在项目文件夹中打开 PowerShell，运行下面的命令即可精简安装。以后需要 USB 控制时，再运行 `install.bat`。

     ```powershell
     .\install.bat --mirror-only
     ```

安装脚本会在 `%LOCALAPPDATA%\iCast\venv` 创建独立的 Python 环境，并在桌面和开始菜单添加 **iCast** 快捷方式。也可以在项目文件夹的 PowerShell 中运行 `.\run.bat` 启动 iCast。如果移动了项目文件夹，请重新运行 `install.bat` 更新快捷方式。

### 让智能助手安装

将下面的提示词粘贴到能直接访问这台 Windows 电脑终端的智能助手中。如果还想用鼠标和键盘控制 iPhone，将模式改为 `mirror-and-usb`。

~~~text
请在这台 Windows 电脑上以 mirror-only 模式安装 iCast。
从 https://github.com/phh235/icast-windows 获取源码。如果电脑上已有此仓库的副本，
就复用它。否则，在我的用户目录下选择一个长期保留的位置，例如 %USERPROFILE%\Projects，
然后在该位置运行以下命令。
git clone https://github.com/phh235/icast-windows.git
如果电脑没有 Git，请下载并解压下面的源码 ZIP 文件。
https://github.com/phh235/icast-windows/archive/refs/heads/main.zip
打开获取到的项目文件夹，阅读 README.md，并在该文件夹中执行下面的步骤。

1. 检查 PATH 上的 python.exe 是否为 Python 3.11 或更高版本。如果缺少，
   从官方来源安装，刷新终端的 PATH 后重新检查。尽量复用已有的 iCast
   环境，并保留我的设置。
2. 自行运行对应的安装命令。将输入重定向到 NUL，安装脚本就不会等待按键。
   cmd.exe /d /c "install.bat --mirror-only < NUL"
   cmd.exe /d /c "install.bat < NUL"
   检查退出码，然后使用 %LOCALAPPDATA%\iCast\venv\Scripts\python.exe
   运行 -m pip check。
3. 使用 .\run.bat 启动 iCast。通过窗口或 %LOCALAPPDATA%\iCast\icast.log
   中本次启动的日志确认接收器已就绪。如果 Windows 询问专用网络访问权限，
   请让我确认。再请我在 iPhone 的「屏幕镜像」中选择接收器，以测试连接。
4. 如果选择 mirror-and-usb，按 README 中的链接安装缺少的组件。如果 Windows
   要求管理员权限，请让我确认。运行 .\control.bat doctor 并处理第一个失败的
   检查项。如果需要安装 WDA 或更新签名，运行
   .\control.bat get-wda 并告诉我 IPA 的准确路径。连接或解锁 iPhone、
   轻点「信任」、在 Sideloadly 中登录和签名、开启开发者模式等操作
   需要时再交给我。如果找不到开发者模式，运行 .\control.bat reveal-dev-mode。
   等我确认后，再运行 doctor。镜像开始后，用 Ctrl+U 开启 USB 控制，
   并检查按钮是否变为绿色。
5. 自行完成能在终端执行的步骤。需要我操作时，说明具体步骤，等我确认后
   继续。最后报告已安装的组件、已验证的功能和仍需处理的问题。
~~~

Apple 账户密码、验证码和 iPhone 密码请直接在 Sideloadly 或设备上输入。[USB 配置详情](#通过-usb-控制)

## 屏幕镜像

1. 启动 **iCast**。准备就绪后，窗口会显示 **Waiting for iPhone**。
2. 如果 Windows 防火墙询问是否允许 Python 访问网络，勾选 **专用网络（Private networks）** 并允许访问。在 Windows 网络设置中，将当前 Wi‑Fi 的网络配置文件类型设为 **专用（Private）**。
3. 在 iPhone 上打开 **控制中心 → 屏幕镜像**，选取电脑名称。要修改列表中显示的名称，在 iCast 窗口中单击右键，然后选择 **Rename PC...**。[Apple 隔空播放指南](https://support.apple.com/zh-cn/102661)

iCast 使用无边框窗口，默认置顶，并会记住窗口位置。iPhone 切换到横屏时，窗口会跟着切换。再次启动 iCast 会将已打开的窗口移到前面。

### 窗口操作和快捷键

将鼠标指针移到窗口内即可显示顶部工具栏。工具栏显示设备名称和帧率，并提供 USB 控制、返回主屏幕、置顶、最小化和关闭按钮。下文保留 iCast 中的英文按钮和菜单名称，方便在界面中查找。

| 操作 | 效果 |
|---|---|
| USB 控制关闭时拖动窗口 | 移动窗口 |
| USB 控制关闭时双击窗口 | 在大窗口和常规大小之间切换 |
| `Ctrl` + 鼠标滚轮，或 `Ctrl` + `+` / `-` | 调整窗口大小 |
| `Ctrl` + `M` | 将电脑上的镜像声音静音或取消静音 |
| `Ctrl` + `U` | 开启或关闭 USB 控制 |
| `Ctrl` + `V` | USB 控制就绪后，将 Windows 剪贴板中的文本粘贴到 iPhone 当前输入框 |
| `Ctrl` + `H` | USB 控制就绪后返回 iPhone 主屏幕 |
| `Ctrl` + `J` | USB 控制就绪后，在配备主屏幕按钮的 iPhone 上打开 App 切换器 |
| `Ctrl` + `↑` / `↓` | USB 控制就绪后调高或调低 iPhone 音量 |

使用面容 ID（Face ID）的 iPhone 可以通过手机上的屏幕底部手势或「辅助触控」打开 App 切换器。在这些机型上按 `Ctrl` + `J`，iCast 会显示辅助触控提示。[Apple 辅助触控指南](https://support.apple.com/zh-cn/111794)

单击右键可以打开菜单。**Always on Top** 用于置顶，**Size** 用于选择窗口大小，**Sound** 用于切换声音，**Save Screenshot** 将截图保存为 `Pictures\iCast` 下的 PNG 文件，**Open Settings Folder** 打开 `settings.json` 所在的文件夹，**Open Log** 打开日志，**Quit** 退出应用。

**Stream Info...** 显示实际接收到的分辨率、显示尺寸和帧率。

## 通过 USB 控制

iCast 通过 iPhone 上运行的 WebDriverAgent（WDA）发送轻点、轻扫和键盘输入。你需要一根 USB 数据线、从 Apple 官网下载的 iTunes（为 Sideloadly 提供所需的 USB 驱动）以及一个 Apple 账户（Apple ID）。免费账户也可以使用。[下载 iTunes](https://www.apple.com/itunes/) · [WebDriverAgent](https://github.com/appium/WebDriverAgent)

### 首次配置

在项目文件夹中打开 PowerShell。`control.bat` 会自动使用 iCast 的 Python 环境。

1. **连接 iPhone。** 安装 iTunes，用数据线连接 iPhone，解锁手机，并在手机询问是否信任此电脑时轻点 **信任**。
2. **生成 WDA 安装包。** 运行下面的命令。`WebDriverAgentRunner.ipa` 默认保存在 Downloads（下载）文件夹。

   ```powershell
   .\control.bat get-wda
   ```

   iCast 下载指定版本的 WDA，校验 SHA‑256，并移除可能导致 Sideloadly 签名失败的 `.dSYM` 调试符号包。IPA 保留了 XCTest 测试运行器和必要的框架，可以直接交给 Sideloadly。[WDA 发布版本](https://github.com/appium/WebDriverAgent/releases/tag/v16.15.3)
3. **签名并安装 WDA。** 打开 Sideloadly，选取 iPhone 和 IPA 文件，输入 Apple 账户，然后单击 **Start**。签名仍取决于 WDA 版本、Sideloadly 和 Apple 的签名要求。免费账户的签名有效期为 7 天，到期后请用 Sideloadly 重新安装。[下载 Sideloadly](https://sideloadly.io)
4. **信任开发者描述文件。** 在 iPhone 上前往 **设置 → 通用 → VPN 与设备管理**，选择用于签名的账户，再按屏幕提示完成信任操作。
5. **在 iOS 16 及更高版本上开启开发者模式。** 前往 **设置 → 隐私与安全性 → 开发者模式**。开启后重新启动 iPhone，解锁并再次确认。如果设置里没有这个选项，运行下面的命令。

   ```powershell
   .\control.bat reveal-dev-mode
   ```

6. **检查配置。** 运行下面的命令。如果有检查项失败，按提示处理。

   ```powershell
   .\control.bat doctor
   ```

7. **开始控制。** 先镜像通过 USB 连接的那台 iPhone，再按 `Ctrl` + `U`、单击手形图标，或在右键菜单中选择 **Control iPhone (USB)**。启动时按钮会以橙色闪烁，就绪后变为绿色。首次启动可能需要下载并挂载开发者磁盘映像，因此会多等一会儿。

控制时请保持 USB 连接。iCast 会核对 USB 连接的 iPhone 与正在镜像的设备是否一致。停止镜像或切换镜像设备后，控制也会结束。

使用 iOS 17.0–17.3 时，需要先以管理员身份运行 `pymobiledevice3 remote tunneld`，再开启控制。iOS 17.4 及更高版本，以及 iOS 16 及更早版本，无需管理员权限即可启动 WDA。

### 连接后的操作

| 在 iPhone 画面区域的操作 | iPhone 上的效果 |
|---|---|
| 单击 / 双击 | 轻点 / 轻点两下 |
| 按住鼠标至少 0.5 秒且不移动 | 长按 |
| 拖动后松开 | 从起点轻扫到松开的位置 |
| 滚动鼠标滚轮 | 滚动页面 |
| 键盘输入 | 输入文字，支持 Enter 和 Backspace |
| 拖动工具栏，或按住 `Alt` 拖动 | 移动 iCast 窗口 |

### 辅助命令

运行 `.\control.bat COMMAND`。在命令后加上 `--help` 可以查看参数。

| 命令 | 用途 |
|---|---|
| `doctor` | 检查 USB 控制环境。加上 `--ip ADDRESS` 可通过 Wi‑Fi 检查正在运行的 WDA |
| `reveal-dev-mode` | 让「开发者模式」开关显示在设置中 |
| `get-wda` | 生成 `WebDriverAgentRunner.ipa`。用 `--out PATH` 指定保存位置 |
| `start` | 从命令行通过 USB 启动 WDA，按 `Ctrl` + `C` 停止 |
| `tap X Y` | 在 iPhone 上轻点指定坐标，单位为点 |
| `home` | 返回主屏幕 |
| `type TEXT` | 向当前选中的输入框输入文字。`\n` 表示回车，`\b` 表示退格 |

`tap`、`home` 和 `type` 默认连接 `http://127.0.0.1:8100`。如果已运行 `start`，请通过 `--url` 传入该命令输出的本地地址。

## 故障排查

| 问题 | 处理方法 |
|---|---|
| 「屏幕镜像」列表里找不到电脑 | 确认两台设备处于同一 Wi‑Fi 网络，Windows 将此网络设为 **专用**，且防火墙允许 `pythonw.exe` / `python.exe` 访问专用网络。访客网络、路由器的客户端隔离或 VPN 都可能阻碍设备发现。重启 iCast 可以重新广播 AirPlay 接收服务。 |
| **Could not decrypt the video** | 在 iPhone 上停止镜像后重新连接。如果反复出现，选择 **Open Log**，并在反馈问题时附上日志。 |
| **iPhone is locked** | 解锁 iPhone 后继续镜像。 |
| 没有声音 | 检查 **Sound** 或按 `Ctrl` + `M`，然后确认 Windows 的音频输出设备和 iPhone 音量。 |
| 无法开启控制 | 运行 `.\control.bat doctor`，解锁 iPhone，并确认已信任开发者描述文件。签名过期后，请用 Sideloadly 重新安装 IPA。 |
| iCast 提示 WDA 版本不匹配 | 查看提示中的 App 捆绑包标识符（bundle ID）和版本。如果选中的安装项是旧版，移除旧版后使用 `get-wda` 生成的版本重试。 |

日志保存在 `%LOCALAPPDATA%\iCast`。应用日志为 `icast.log`，WDA 运行日志为 `wda_runner.log`，最近一次已结束的 USB 端口转发日志为 `wda_forward.log`。

## 设置和限制

在右键菜单中分别选择 **Resolution**（720p、1080p 或 1440p）和 **Frame Rate**（30、60 或实验性 120 FPS）。重新连接屏幕镜像后会发送新的请求。**Stream Info...** 显示接收到的分辨率和显示帧率，具体取决于 iPhone 发送的视频流。

首次启动 iCast 时会创建 `%LOCALAPPDATA%\iCast\settings.json`。请先退出应用再编辑该文件。

| 配置项 | 默认值 | 用途 |
|---|---|---|
| `name` | 电脑名称 | 「屏幕镜像」列表中显示的名称 |
| `width`、`height` | `1920`、`1080` | iCast 向 iPhone 请求的视频流尺寸 |
| `fps` | `60` | 请求的帧率 |
| `audio` | `true` | 在电脑上播放 iPhone 的声音 |
| `always_on_top` | `true` | 保持 iCast 窗口置顶 |
| `phone_height` | `0`（自动） | iPhone 画面区域的长边长度，单位为 Windows 逻辑像素 |

- **清晰度。** 可以尝试设置 `"height": 1440`。这是请求值，实际清晰度取决于 iPhone 发送的视频流和显示大小。如果 iPhone 无法连接，改回 `1080`。
- **受 DRM 保护的内容。** 镜像 Netflix、Apple TV 等应用时，受保护的画面可能显示为黑屏。
- **控制延迟。** WDA 指令通常需要约 100–200 毫秒。单击后，iCast 会多等约 0.2 秒来区分单击和双击。轻扫在鼠标松开时才会发送，因此物体无法跟随指针实时拖动，也无法实时绘画。

配置文件还记录窗口位置和 AirPlay 接收器标识。删除该文件会重置全部配置。之后，iPhone 会将 iCast 识别为新的接收器。

## 开发与测试

iCast 通过 mDNS 宣告 AirPlay 服务，并用 RTSP 建立会话。PyAV/FFmpeg 解码 H.264 视频和 AAC-ELD 音频，PySide6 绘制界面，sounddevice 播放音频。USB 控制由 pymobiledevice3 启动 WDA，并通过本机回环端口转发 HTTP 请求。

请在项目文件夹中运行命令。可以使用 `%LOCALAPPDATA%\iCast\venv\Scripts\python.exe`，也可以在其他 Python 3.11+ 环境中安装 `requirements.txt`。要运行 pymobiledevice3 相关检查，请安装 `requirements-control.txt` 中的依赖。iPhone 模拟器还需要 NumPy。

```powershell
python tests/test_fairplay.py
python -m unittest discover -s tests -q
```

如果没有 iPhone，可以用模拟器测试镜像。在两个 PowerShell 窗口中分别运行下面的命令，先启动接收器，再运行模拟器。

```powershell
python -m icast --local
python tools/fake_iphone.py 127.0.0.1 7100
```

`--local` 只监听本机，使用单独的配置文件，并且不会广播 mDNS。其他选项包括 `--port N`（更改 RTSP 端口）、`--control`（开始镜像后开启 USB 控制）、`--grab DIR`（每 2 秒保存一张 PNG）和 `--quit-after N`（N 秒后自动退出）。

| 路径 | 内容 |
|---|---|
| `icast/airplay.py`、`icast/discovery.py` | RTSP 会话和 AirPlay 的 mDNS 服务发现 |
| `icast/pairing.py`、`icast/fairplay/` | AirPlay 配对与 FairPlay 处理 |
| `icast/video.py`、`icast/audio.py` | 视频和音频的解码与播放 |
| `icast/ui.py`、`icast/config.py` | iCast 窗口与配置 |
| `icast/control/` | USB 控制、WDA 和 IPA 打包 |
| `tests/`、`tools/` | 测试、iPhone 模拟器和 FairPlay 代码生成工具 |

要从 UxPlay 中的 playfair 源码重新生成 `_tables.py` 和 `_garble.py`，请运行 `python tools/gen_fairplay.py path\to\UxPlay\lib`。

## 致谢与许可证

iCast 参考了 UxPlay、RPiPlay 和 shairplay。依赖库和测试数据来源见[英文版致谢](README.md#credits)。

应用整体以 [GPL-3.0](LICENSE) 许可证发布。`icast/fairplay/` 模块移植自 [playfair](licenses/playfair-LICENSE.md)。版权和本版本的修改说明见 [NOTICE](NOTICE)。

iCast 是独立项目，与 Apple Inc. 无关联。AirPlay、iPhone、FairPlay 和 iOS 是 Apple Inc. 的商标。请仅镜像你有权使用的设备和内容。
