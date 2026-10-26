# Azure DevOps AI Bot 框架 (OpenCode Container-Native 版)

本專案是基於 **OpenCode CLI** 的通用 SDLC 自動化機器人執行引擎。  
採用 **「外部極簡單行 Runner + 內部 `.opencode/` 純 Markdown 彈性自治」** 架構：Agent 開發者無需修改任何 Python 腳本，只需維護 `.opencode/` 下的 Markdown 檔案即可擴充角色與規範。

---

## 專案目錄結構

```text
ado-bot/
├── .opencode/                        # 【Agent 開發者核心工作區】透過 OPENCODE_CONFIG_DIR 掛載
│   ├── opencode.json                 # 極簡無頭容器設定 (關閉互動詢問、限制龐大 log 截斷、自動壓縮)
│   ├── agents/                       # SDLC 主控階段與專職子代理 (純 Markdown)
│   │   ├── clarify.md                # [Primary] 階段 1：需求釐清與範圍盤點 -> 01_clarify.md
│   │   ├── spec.md                   # [Primary] 階段 2：SA 規格 (AC 編號)、API 契約與設計 -> 02_spec.md
│   │   ├── dev.md                    # [Primary] 階段 3：研發交付主控 (實作 + 調度測試/審查 + 自癒至綠燈)
│   │   ├── explore.md                # [Subagent] 唯讀探索程式碼庫，隔離搜尋噪音
│   │   ├── tester.md                 # [Subagent] 獨立視角對照 AC 撰寫 JUnit 5 測試
│   │   └── reviewer.md               # [Subagent] 獨立黑臉審查員 (高推理強度唯讀把關)
│   ├── skills/
│   │   └── spring-boot-dev/SKILL.md  # Spring Boot + Maven/Gradle + JUnit 5 開發規範 (按需載入)
│   └── plugins/
│       └── anti-cheat.ts             # 底線護欄：攔截 -DskipTests / @Disabled 等跳過測試行為
├── orchestrator/
│   └── prototype.py                  # WIQL 看板輪詢與 GitLab Pipeline 派工
├── agents/
│   └── base_agent.py                 # 極簡 Container Runner (Clone -> 單行 opencode run -> 出口驗收 -> Push)
└── .gitlab-ci.yml                    # CI 排程與 JDK 21 + OpenCode 容器執行定義
```

---

## SDLC 看板映射與執行機制

外部 Runner (`agents/base_agent.py`) 對所有階段皆執行**同一行標準指令**：

```bash
OPENCODE_CONFIG_DIR=/path/to/ado-bot/.opencode \
opencode run \
  --dir /workspace \
  --agent "$AGENT_NAME" \
  --dangerously-skip-permissions \
  -f /tmp/wi-1024-context.md \
  "請針對附件的 Work Item #1024 完成 $AGENT_NAME 階段交付"
```

| 看板欄位 | `AGENT_NAME` | 交付行為與品質把關 (Harness) |
| :--- | :--- | :--- |
| `Clarify` | `clarify` | 委派 `@explore` 盤點現有模組 ➔ 產出 `docs/features/wi-{id}/01_clarify.md` ➔ `@reviewer` 審查 |
| `Spec` / `Requirement` | `spec` | 產出含 `AC-01..N` 追蹤碼的 `02_spec.md`（及選配 `03_openapi.yaml`）➔ `@reviewer` 審查 |
| `Implement` | `dev` | 支援 Feature 與 Bugfix 雙軌：實作 `src/main` ➔ 委派 `@tester` 獨立寫 `src/test` ➔ 跑測試自癒至綠燈 ➔ `@reviewer` 終審 ➔ Push 前由 Python 執行最後一道 `mvn/gradle test` 出口驗收 |

---

## Agent 開發者本地互動除錯方式

想在本地目標 Java 專案直接測試或調整 `.opencode/agents/*.md` 時，無需透過 ADO，直接在目標專案目錄啟動 TUI 即可獲得與 CI 100% 一致的行為：

```bash
cd /path/to/your-spring-boot-repo
OPENCODE_CONFIG_DIR=/path/to/ado-bot/.opencode opencode --agent dev
```
