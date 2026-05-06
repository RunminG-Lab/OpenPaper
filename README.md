# 📚 OpenPaper

**为研究生打造的本地论文管理与速读工具**

把 PDF 扔进文件夹，剩下的交给 OpenPaper：自动解析元信息、分组整理、全文检索、可视化统计，一键调用 AI 生成结构化速读摘要。无需数据库、无需注册账号，全部数据本地存储。

![主界面 — 分组浏览 + 顶部统计条 + 多维筛选](_docs/screenshot-main.png)

---

## ✨ 核心功能

| 功能模块 | 说明 |
|---|---|
| **自动入库** | 把 PDF 丢进 `papers/` 子目录，监控进程自动触发重建，刷新浏览器即可看到 |
| **智能识别** | 从文件名自动推断会议/期刊（ICSE、ASE、CVPR、NeurIPS、ACL 等 60+ 顶会）和年份 |
| **分组整理** | 按子目录自动分组，支持拖拽排序分组；也可切换到平铺模式全局浏览 |
| **检索筛选** | 实时搜索标题/作者/标签；按会议、年份、分类标签（OR/AND）多维筛选 |
| **阅读管理** | 标记已读/未读，一键只看未读，在资源管理器中定位 PDF 原文件 |
| **可视化统计** | 内置独立统计面板（`stats.html`），展示年份分布、会议分布等图表 |
| **AI 速读** | 对接 OpenAI 兼容接口（支持 VectorEngine、DeepSeek、本地 Ollama 等），生成结构化速读摘要，结果本地缓存 |
| **回收站** | 删除论文移入本地回收站，支持恢复或永久清除 |
| **开机自启** | 一行命令注册 Windows 任务计划，登录后自动在后台启动服务 |

### 多维检索与分类筛选

> 支持文本搜索 + 会议筛选 + 标签 OR/AND 交叉过滤，快速定位目标论文。

![](_docs/screenshot-filter.png)

### 可视化统计面板

> 独立页面展示论文库全貌：累计增长曲线、月度新增、标签分布、已读进度……

![](_docs/screenshot-stats.png)

---

## 🚀 快速上手（5 分钟）

### 第一步：克隆仓库

```bash
git clone https://github.com/TsingPig/OpenPaper.git
cd OpenPaper
```

### 第二步：安装依赖

```bash
pip install watchdog
```

> **最低要求**：Python 3.8+，无其他强依赖。

### 第三步：放入论文

把 PDF 文件放进 `papers/` 目录下的**任意子文件夹**（子文件夹名就是分组名）：

```
papers/
  LLM/
    attention-is-all-you-need-nips17.pdf
    gpt4-technical-report-arxiv23.pdf
  GUI Testing/
    uiautomator2-ase22.pdf
  unsorted/
    some-paper-without-category.pdf
```

### 第四步：启动服务

```bash
python waatchdog.py
```

服务启动后会自动构建 `index.html`，并在后台监控 `papers/` 目录的变化。

### 第五步：打开浏览器

访问 **http://127.0.0.1:8000**，即可看到完整的论文管理界面。

---

## 📂 文件命名规范（让元信息自动识别更准确）

文件名中包含会议缩写+年份后缀，系统会自动识别会议和年份：

```
论文标题关键词-icse24.pdf          → ICSE 2024
attention-is-all-you-need-nips17.pdf → NeurIPS 2017
gpt4-technical-report-arxiv23.pdf   → arXiv 2023
some-paper-ase2023.pdf              → ASE 2023
```

**已支持的顶会/顶刊（部分）**：
- 软件工程：ICSE、ASE、FSE/ESEC、ISSTA、ICSME、MSR、TSE、TOSEM
- AI / ML：NeurIPS、ICML、ICLR、AAAI、IJCAI
- NLP：ACL、EMNLP、NAACL
- 计算机视觉：CVPR、ICCV、ECCV
- 安全：S&P、CCS、USENIX Security、NDSS
- 人机交互：CHI、UIST
- 机器人/仿真：ICRA、IROS

文件名识别失败时，会议和年份字段留空，可在界面中手动编辑补充。

---

## ⚡ AI 速读功能配置

点击工具栏右侧的 **⚙️ 设置** → 切换到 **AI 速读** 标签页，填入以下信息：

![](_docs/screenshot-settings-speedread.png)

| 字段 | 说明 | 示例 |
|---|---|---|
| API Base URL | 任何 OpenAI 兼容接口的地址 | `https://api.deepseek.com/v1` |
| API Key | 对应平台的密钥 | `sk-xxxxxxxx` |
| 模型 | 支持视觉的模型效果更好 | `deepseek-chat` / `gpt-4o` |
| 超时（秒） | 默认 60 秒，长文建议调大 | `120` |

配置完成后点击 **测试连通性** 确认接口可用，然后在任意论文卡片上点击 **⚡ 速读** 即可生成。

**推荐模型（国内可用）**：
- [DeepSeek](https://platform.deepseek.com/)：`deepseek-chat`，性价比极高
- [VectorEngine](https://vectorengine.cn/)：`gpt-4o`，支持多模态
- 本地 [Ollama](https://ollama.com/)：`http://127.0.0.1:11434/v1`，无需联网

速读结果持久化缓存在 `.speedread_cache/` 目录，重新打开页面无需再次生成。若需刷新，点击速读面板内的 **重新生成** 按钮。

### 速读摘要结构

> AI 速读面板包含：一句话总结、快速省流、问题与动机、方法速读、**核心图表解读（含 PDF 原图）**、实验速读、贡献与局限、精读建议。

![](_docs/screenshot-speedread-top.png)

> **核心图表解读**：AI 自动定位关键页面截图，逐图说明「看什么」「说明了什么」「为什么重要」。

![](_docs/screenshot-speedread-figures.png)

---

## 🔄 开机自启（可选）

在 Windows 上注册任务计划，登录后自动后台启动服务：

```powershell
# 以普通用户权限运行（不需要管理员）
powershell -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1
```

卸载自启：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall_autostart.ps1
```

注册成功后日志输出到 `waatchdog.log`，方便排查问题。

---

## 📁 目录结构说明

```
OpenPaper/
├── papers/              # 论文存放目录（子目录名 = 分组名，本地不入 git）
│   ├── demo/            # ✅ 示例论文（随仓库分发，供快速体验）
│   │   └── nips20_Retrieval-Augmented_Generation_for_Knowledge-Intensive_NLP_Tasks.pdf
│   ├── LLM/
│   ├── GUI Testing/
│   └── ...              # 其他分组，本地自行添加
├── template.html        # 前端源文件（修改此文件而非 index.html）
├── index.html           # 由 build.py 生成，勿手动编辑
├── stats.html           # 独立统计面板
├── build.py             # 从 template.html + metadata.json 生成 index.html
├── waatchdog.py         # 本地服务：HTTP Server + 文件监控 + API
├── metadata.json        # 所有论文的元数据（笔记、标签、已读状态等）
├── favicon.svg          # 网站图标
├── scripts/
│   ├── install_autostart.ps1    # 注册开机自启（Windows）
│   ├── uninstall_autostart.ps1  # 卸载开机自启
│   ├── start_waatchdog.vbs      # 静默启动脚本（无黑色控制台）
│   └── fix_metadata.py          # 一次性修复工具：路径漂移时重建 metadata 索引
├── _docs/               # 文档截图资源
└── .speedread_cache/    # AI 速读结果缓存（自动创建，不入 git）
```

---

## 🛠️ 常见问题

**Q：放入 PDF 后页面没有更新？**  
A：确认 `waatchdog.py` 正在运行，手动刷新浏览器（F5）。如果还没有，检查 `waatchdog.log` 查看构建日志。

**Q：会议/年份识别不对怎么办？**  
A：点击论文卡片右上角的编辑按钮，手动修改会议、年份、标题等字段，修改后自动保存到 `metadata.json`。

**Q：AI 速读提示"连接失败"？**  
A：先点击设置页的「测试连通性」，确认 Base URL 和 API Key 正确。国内网络访问 OpenAI 官方接口时可能需要代理，推荐使用 DeepSeek 或 VectorEngine 等国内接口。

**Q：多台设备共享怎么做？**  
A：把整个仓库放在局域网共享盘或同步盘（OneDrive、Nutstore 等）中，`metadata.json` 和 `.speedread_cache/` 会随之同步；`papers/` 目录默认被 `.gitignore` 排除，各自本地存放 PDF 即可。

**Q：想自定义前端样式怎么做？**  
A：编辑 `template.html`，修改完成后运行 `python build.py` 重新生成 `index.html`，刷新浏览器生效。

---

## 📋 依赖列表

```
watchdog>=2.0
```

PDF 文本提取使用系统级工具（如 `pdftotext`），如未安装则自动回退到纯文本模式进行速读。

---

## 📜 License

MIT
