# Azure DevOps AI Bot 框架 (ADO Bot Framework)

本專案是通用型 SDLC AI 自動化機器人的**工具與執行引擎 (Runner Engine)**。
負責透過 **GitLab CI Scheduled Pipeline** 定期以 **WIQL (Work Item Query Language)** 取得 ADO 看板狀態，並調度各階段 AI Agent **動態至各業務應用儲存庫 (Target Application Repo)** 進行規格產出、程式碼實作與單元測試。

---

## 專案結構 (工具儲存庫 ado-bot)

```tree
ado-bot/ (本工具儲存庫：保持純淨，不含業務代碼)
├── docs/
│   └── ado-ai-bot-framework-blueprint.md  # 完整的架構設計藍圖與通訊協定規範 (v2.1 解耦版)
├── orchestrator/
│   └── prototype.py                       # Orchestrator 核心（WIQL 調和、派工目標業務 Repo、回滾保護）
├── agents/
│   ├── base_agent.py                      # Agent 基礎類別（動態 clone 目標業務 Repo 至 /workspace、Docs-as-Code、Git 提交）
│   └── clarify_agent.py                   # 需求釐清 Agent 實作範例
├── .gitlab-ci.yml                         # GitLab CI/CD Pipeline 配置
└── README.md
```

---

## 雙儲存庫解耦架構 (Runner vs Target Repo)

- **工具儲存庫 (`ado-bot`)**：
  - 存放共用的 Orchestrator 巡邏邏輯與 Agent 核心 Prompts。
  - 不存放任何業務程式碼或專案規格。
- **目標業務儲存庫 (`Target Application Repo`)**：
  - 例如 `order-service`、`crm-web`（可位於 GitLab 或 ADO Repos）。
  - Agent 在 CI 執行時動態 Clone 目標 Repo 至 `/workspace`：
    - 規格存放：`workspace/docs/features/wi-{id}/...`
    - 代碼存放：`workspace/src/...`
    - 測試存放：`workspace/tests/...`
  - 完成後直接推播回目標 Repo 的 `feat/wi-{id}` 分支。

---

## 本地測試與模擬方式

### 1. 安裝必要套件
```bash
pip install requests
```

### 2. 設定環境變數
```bash
# ADO 連線設定
export ADO_ORGANIZATION="<你的-ado-組織名稱>"
export ADO_PROJECT="<你的-ado-專案名稱>"
export ADO_PAT="<個人存取權杖-Personal-Access-Token>"

# GitLab CI 連線設定
export GITLAB_URL="https://gitlab.example.com"
export GITLAB_TOKEN="<gitlab-trigger-token>"
export GITLAB_PROJECT_ID="<gitlab-project-id>"
export GITLAB_DEFAULT_REF="main"

# 目標業務儲存庫 (可選：若未指定則在本地目錄模擬)
export TARGET_REPO_URL="https://gitlab.example.com/backend/order-service.git"
```

### 3. 執行 Orchestrator
```bash
python orchestrator/prototype.py
```

### 4. 單獨測試特定 Agent (例如 Clarify Agent)
```bash
export WORK_ITEM_ID="1024"
export STAGE="clarify"
export TARGET_BRANCH="feat/wi-1024"

cd agents
python clarify_agent.py
```
Agent 會動態拉取目標業務專案至 `/workspace`，將規格寫入 `workspace/docs/features/wi-1024/01_clarify.md` 並推播回目標專案分支。
