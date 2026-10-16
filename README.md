# ScreenPersonMonitor 屏幕人物监视截图工具

屏幕上一旦有人经过指定位置，就自动把指定区域的画面截图保存下来。

---

## 使用流程（点开 exe 后按提示走三步）

| 步骤 | 操作 |
|---|---|
| **第 1 步** | 弹出文件夹选择框 → 选择截图要保存到哪个文件夹（直接取消 = 保存到 exe 旁边的 `screenshots` 文件夹） |
| **第 2 步** | 屏幕变成半透明遮罩 → 按住鼠标左键拖拽，框选**【截图区域】**（有人经过时要保存下来的那块画面）→ 回车确认 |
| **第 3 步** | 再次出现遮罩 → 拖拽框选**【监控区域】**（用来判断有没有人的位置）→ 回车确认 |

框完之后程序自动加载模型并开始监视。

### 预览窗说明（全中文界面）

预览窗顶部有一条很窄的状态条，显示当前状态：

| 显示文字 | 含义 |
|---|---|
| 绿色 `监视中 · 已存 N 张` | 正常监视中，已保存 N 张截图 |
| 红色 `检测到人 · 正在截图` | 监控区域里检测到人，正在抓取截图区域保存 |
| 橙色 `已锁定 · 暂停监视` / `解锁后自动继续` | 屏幕被锁屏（Win+L），系统禁止抓屏，锁定期间暂停；解锁后自动恢复 |
| 检测框上的 `人 87%` | 画面里检测到人的位置和置信度（87% = 0.87） |

> 状态条高度只有 46 像素（画面很小时还会自动缩短甚至不显示），尽量少遮挡监控画面。
> 中文字体使用系统的微软雅黑；万一系统缺字体，界面会自动退回英文，不影响功能。

### 退出方式

**按 Q 或 Esc 退出**（点预览窗右上角的 × 也能退出）。

### 框选时的按键

| 按键 | 作用 |
|---|---|
| 鼠标左键拖拽 | 画选框 |
| 鼠标右键 | 重画 |
| 回车 | 确认这个区域 |
| Esc | 取消，程序退出 |

> 两个区域是**互相独立**的：可以框一个很大的区域用于保存（例如整个门口），
> 再框一个很小的区域用于监控（例如门把手附近），只有监控区域里出现人才会触发。

---

## 排查问题

程序**不会**在保存文件夹里生成任何日志文件，只往里写截图（`person_年月日_时分秒.png`）。
运行时信息打印在控制台（用源码 `python main_opencv.py` 运行时可看到；双击 exe 无控制台，这些输出会被丢弃）。

如果出现"有人经过但没截图"，按下面顺序排查：

| 可能原因 | 现象 | 怎么处理 |
|---|---|---|
| 监控区域没框到人 | 预览窗里没有黄色检测框 | 把监控区域框得更贴近人 |
| 人太小 / 距离太远 | 预览窗有黄色框，但置信度低于 45% | 把监控区域框得更紧，或让人物在画面里更大 |
| 屏幕被锁屏（Win+L） | 预览窗顶部显示橙色 `已锁定 · 暂停监视` | 解锁后自动恢复；想持续监控就别锁屏 |

> **锁屏（Win+L）后无法抓屏**：Windows 会切到安全桌面，这是系统安全设计，任何普通程序都做不到。
> 程序会自动暂停并在解锁后恢复，不会崩溃。

如果怀疑程序本身有问题，用下面的诊断命令验证：

```bash
python main_opencv.py --probe-dpi                  # 打印 DPI / 屏幕尺寸
python main_opencv.py --test-detect 图片路径        # 对一张图片跑检测, 看能否识别人
python main_opencv.py --selftest 保存目录           # 跳过框选, 监视全屏 30 秒
python main_opencv.py --locktest 保存目录           # 锁屏测试, 监视全屏 120 秒
```

### 生成的文件

| 文件 | 位置 | 说明 |
|---|---|---|
| `person_年月日_时分秒.png` | 你选的保存文件夹 | 截图（唯一会生成的文件） |
| `debug_first_frame.png` 等 | `%TEMP%\ScreenPersonMonitor_debug\` | 仅自检/锁屏测试模式下的调试图，正常使用不会生成 |


---

## 工作原理

- **检测**：YOLOv8n 模型（`yolov8n.pt`），只关心 COCO 类别 0 = person，置信度阈值 0.45
- **抓屏**：`mss`，已开启 DPI 感知（每显示器 v2），高 DPI 缩放/多屏下坐标不偏移
- **框选覆盖层**：tkinter 全屏无边框半透明窗口，坐标与物理像素 1:1 对应
- **保存**：`cv2.imwrite` 存 PNG，文件名 `person_%Y%m%d_%H%M%S.png`
- **冷却**：默认 5 秒一张，同一个人一直停留也不会刷爆文件夹

---

## 命令行诊断工具

下面这些命令主要给排查问题用，正常使用直接双击 exe 即可：

```bash
python main_opencv.py --probe-dpi                  # 只打印 DPI / 屏幕尺寸信息
python main_opencv.py --test-detect 图片路径        # 对一张图片跑一次检测, 打印原始结果
python main_opencv.py --selftest 保存目录           # 跳过框选, 直接监视全屏 30 秒
python main_opencv.py --locktest 保存目录           # 锁屏测试: 监视全屏 120 秒, 期间可 Win+L 验证
```

打包后的 exe 也支持这些参数，例如：

```powershell
.\ScreenPersonMonitor.exe --probe-dpi
```

---

## 直接用源码运行

```bash
pip install -r requirements.txt
python main_opencv.py
```

## 重新打包成 exe

```bash
python -m PyInstaller --noconfirm --clean ScreenPersonMonitor.spec
```

生成的 exe 在 `dist\ScreenPersonMonitor.exe`，把它复制到桌面即可（约 279 MB）。
打完包可以删掉 `build\` 和 `dist\` 回收约 620 MB，不影响已复制出去的 exe。

> **打包注意**：`torchvision` 0.29 的原生扩展是 `_C_stable.pyd` / `image_stable.pyd`，
> 而 PyInstaller 默认只收集 `*.dll` 不收 `*.pyd`。`.spec` 里已经用
> `collect_dynamic_libs('torchvision', search_patterns=['*.pyd', '*.dll'])` 处理好了。
> 如果漏掉，exe 会报 `operator torchvision::nms does not exist`，表现为"有人经过也不截图"。

---

## 文件说明

| 文件 | 说明 |
|---|---|
| `main_opencv.py` | **当前使用的程序**（三步流程：选文件夹 → 选截图区域 → 选监控区域） |
| `ScreenPersonMonitor.spec` | PyInstaller 打包配置（含 torchvision 原生库收集） |
| `yolov8n.pt` | YOLOv8n 模型权重 |
| `requirements.txt` | 依赖清单 |
| `main.py` | 早期版本（只框选一个区域，保存整屏），保留备用 |
| `main_original_aimbot.py` | 原始仓库代码（AI 自瞄），与本工具无关，保留存档 |
