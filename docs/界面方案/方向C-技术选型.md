# 方向 C 技术选型：pywebview + 本地 HTML

这是一次无人值守的自动化任务运行的选型决定（2026-08-14），按任务说明「不要直接开写，先摆权衡」的要求记录在这里，供你事后确认或推翻。

## 结论：pywebview（本地 HTML/CSS/JS + Python 后端桥接）

## 权衡对比

| | PySide6 / Qt | pywebview + 本地 HTML |
|---|---|---|
| 控件成熟度 | 高（QTableView / QStackedWidget / QSS） | 中（要自己拼 HTML/CSS，但 `docs/界面方案/三套方案视觉稿.html` 已经是现成的深色稿，可以直接改造复用） |
| DPI | 免费拿到 | WebView2 内核本身按 CSS 像素渲染，不需要现在 `theme.py` 里那套「DPI 感知 + px() 换算」，这部分可以整个删掉 |
| exe 体积 / 打包 | 大，PyInstaller 要多收 Qt 插件目录，体积明显上涨 | 轻，见下方依赖清单 |
| 运行时依赖 | 无额外系统依赖 | 依赖系统 WebView2 Evergreen 运行时 |
| 开发速度 | 中 | 快，设计稿的 CSS 基本能直接搬 |

## 验证过的事实（不是猜测）

1. **内网镜像装得上**：本机用仓库 `build.bat` 同款镜像源
   `https://mirrors.aliyun.com/pypi/simple/` 试装，`pywebview` 及其 Windows 后端依赖
   （`pythonnet`、`clr_loader`、`cffi`、`bottle`、`proxy_tools`）全部下载成功，无需额外源。
   同一镜像下 `PySide6` 的元包也能装，但那只是 578KB 的壳，真正的 Qt 二进制
   （PySide6-Essential/Addons，通常 200MB+）没有实际下载验证体积。
2. **WebView2 后端在本机跑得起来**：装完之后用一个自动关闭的最小窗口做了冒烟测试
   （`webview.create_window(...)` → `webview.start()` → 2 秒后自毁），窗口正常创建、
   `loaded` 事件正常触发、进程正常退出，说明本机的 WebView2 Evergreen 运行时可用。
3. **依赖清单很轻**：Windows 下 pywebview 实际拉起的依赖只有
   `pythonnet(1.6MB whl) + clr_loader + cffi + pycparser + bottle + proxy_tools`，
   不涉及任何 Qt/GTK 二进制。

## 还没验证、需要你在同事机器上确认的

- **WebView2 运行时是否人人都有**：Windows 11 默认自带，Windows 10 从 2022 年起
  随 Edge 自动更新也基本都装了，但走的是"内网办公机"这种非标准环境，不能 100% 保证。
  如果打包验证时发现有人机器没有，兜底方案是在 `build.bat` 里加一步检测 + 提示手动装
  Evergreen Bootstrapper（微软官方独立安装包，几 MB，离线可用）。
- **PyInstaller 打包 pywebview 应用能否在没装 Python 的机器上跑起来**——
  这是方向 C 相比方向 B 多出来的主要风险点，需要放到后面打包验证阶段专门测。

## 决定不选 PySide6 的原因

设计稿已经是 HTML/CSS，PySide6 需要把整套视觉稿重新翻译成 QSS + 控件树，
时间成本明显更高；且 exe 体积对「发一个文件夹给同事」这种分发方式不友好。
如果后续打包验证发现 WebView2 运行时缺失是普遍问题，再切回 PySide6 也不迟——
阶段 0（本次做的逻辑收口）和技术栈无关，不会因为换栈而返工。
