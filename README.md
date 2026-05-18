# openlist_strm_monitor
君只见生成strm的工具多样 却不见删除strm后对应源文件的狼狈

[![Python Version](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-windows-lightgrey.svg)]()

---

### 📖 项目简介

在 Emby/Jellyfin 中，我们经常使用 `strm` 文件来挂载云盘资源，避免刮削的时候频繁读取修改云盘文件，既加快刮削的速度(不需要读取mediainfo)也免除了云盘的速率限制或风控。

openlist生成的strm是http url 而非其他工具的路径格式，故而神医助手或者类似的strm多功能插件处理不了此类的 `追删`，`深度删除`

程序实时监控 Windows 本地文件夹，当 `.strm` 文件被删除时，程序会通过本地数据库进行“二次校验”，精准定位 WebDAV 端的原始视频，并执行 **同步删除** 或 **移动至回收站** 的操作。

---

### ✨ 核心功能

*   🚀 **极速增量扫描**：采用数据库索引技术，启动时秒级同步数千个 strm 文件，不产生冗余 IO。
*   实时监控：基于 `watchdog` 毫秒级响应本地文件变动（新增、修改、删除）。
*   二次校验：在本地存储 SQLite 数据库映射。即便 `.strm` 文件已物理消失，程序依然能找回其内容（WebDAV 路径）。
*   智能回收站 (MOVE)：支持保留原始层级的移动操作。
    *   *示例：* `/电影/A/1.mp4` 被删除，会安全移至 `/回收站/电影/A/1.mp4`。
*   openlist webdav：针对openlist，解决了 URL 编码歧义、403 鉴权及 500 内部错误等常见 WebDAV 兼容性问题。
*   轻量绿色部署：支持 Python 嵌入式版本运行，无需安装全局 Python 环境，不污染系统。

---

### 📂 项目结构

```text
.
├── python_embed/                        # Python 嵌入式绿色环境目录
│   └── strm_mapping.db                  # 自动生成的本地路径映射数据库 (SQLite)
├── strm_monitor.py                      # 核心监控 Python 程序
├── config.ini                           # 配置文件 (存储路径、账号及模式)
└── openlist_strm_monitor_debug.bat      # 一键管理控制台 (启动/停止/自启/清理)
```


---

### 🚀 快速开始

#### 1. 环境准备
*   **推荐方式**：下载本项目后，将官方 [Python Windows embeddable](https://www.python.org/downloads/windows/) 版本解压到 `python_embed` 文件夹。
*   **手动安装依赖**（若不使用嵌入版）：
    ```bash
    pip install watchdog requests
    ```
*  顺带一提 pip也要手动装 

#### 2. 填写配置
在项目根目录创建 `config.ini`，参考以下内容：

```ini
[Local]
; 监控的本地 strm 文件夹路径
monitor_folder = D:\Videos\Strm
; 数据库位置 (建议放在脚本目录下)
db_file = ./python_embed/strm_mapping.db

[WebDAV]
; AList WebDAV 根路径 (结尾不要带斜杠)
host = http://192.168.1.10:5243/dav
user = admin
password = your_password

[Setting]
; 操作模式: MOVE (移动到回收站) 或 DELETE (直接删除)
action = MOVE
; 回收站文件夹名称
trash_dir_name = strm_回收站
```

#### 3. 运行管理
双击运行 **`openlist_strm_monitor_debug.bat`**：
- [1] 在控制台运行 (前台显示实时日志, 关闭窗口即停止)
- [2] 在后台静默运行并添加开机自启
- [3] 停止正在后台运行的监控进程 (安全模式)
- [4] 打开开机自启文件夹 (用于手动取消自启)
- [5] 清除本地数据库 (用于环境重置/重新扫描)
- [6] 退出控制台

---

### ⚙️ 配置项详解

| 配置项 | 说明 |
| :--- | :--- |
| **monitor_folder** | 你的 Emby/Jellyfin 正在扫库的那个本地 strm 根目录。 |
| **action** | `MOVE` 模式下程序会尝试保留路径结构；`DELETE` 模式将删除 WebDAV 文件 是否永久删除看openlist挂载的设置。 |
| **trash_dir_name** | 程序会在每个挂载路径的根目录下创建以此命名的文件夹作为“垃圾桶”。 |

---

### ⚠️ 常见问题说明

*   **500 错误**：通常为 WebDAV 路径编码问题，本项目已通过 `safe='/'` 策略最大化兼容。
*   **403 错误**：请检查后台设置，确保元数据未设置“只读”，且账号拥有删除/移动权限。
*   **编码问题**：若本地路径包含中日韩等字符，请务必保证 `config.ini` 以 **UTF-8** 编码保存。

---

### 🤝 致谢

感谢[openlist](https://github.com/OpenListTeam/OpenList) 的支持

---

### 📄 开源协议

本项目采用 [MIT License](LICENSE) 协议。
```
