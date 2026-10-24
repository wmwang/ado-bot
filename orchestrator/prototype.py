"""
ADO AI Bot Orchestrator — 極簡版 (KISS 架構)
============================================
【運作規則】：
1. 不動公司現有看板欄位。
2. 每個階段只看一個標籤：{stage}:done。
   - 卡片在該欄位，且沒有 {stage}:done 也沒有 {stage}:running ➜ 派工！
   - 人類想原地重做 ➜ 直接在卡片上按「✕」刪除 {stage}:done 標籤，系統自動重跑！
3. 人類推進 ➜ 直接把卡片拖到下一個欄位。
"""

import logging
import os
import sys
import requests
from dataclasses import dataclass
from typing import List, Dict, Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("orchestrator")


# =============================================================================
# 1. 欄位與 Agent 對照表 (極簡階段映射)
# =============================================================================

# 看板欄位名稱 -> 對應的 Agent 名稱
# 每個 Agent 醒來以 {agent_name}:running 與 {agent_name}:done 作為狀態標籤
STAGE_MAPPING = {
    "Clarify": "clarify-agent",
    "Requirement": "requirement-agent",
    "Contract": "contract-agent",
    "Implement": "scaffold-agent",
    "Test": "test-gen-agent",
}

ADO_ORG = os.environ.get("ADO_ORGANIZATION", "your-org")
ADO_PROJECT = os.environ.get("ADO_PROJECT", "your-project")
ADO_PAT = os.environ.get("ADO_PAT", "")
ADO_BASE_URL = f"https://dev.azure.com/{ADO_ORG}/{ADO_PROJECT}/_apis"

GITLAB_URL = os.environ.get("GITLAB_URL", "https://gitlab.example.com")
GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN", "")
GITLAB_PROJECT_ID = os.environ.get("GITLAB_PROJECT_ID", "")
GITLAB_DEFAULT_REF = os.environ.get("GITLAB_DEFAULT_REF", "main")

# 目標業務儲存庫（Target Application Repo）：解耦工具腳本與業務專案
# 可為 GitLab Repo 或 ADO Repos，例如 "https://gitlab.example.com/backend/order-service.git"
TARGET_REPO_URL = os.environ.get("TARGET_REPO_URL", "")


# =============================================================================
# 2. ADO 查詢客戶端
# =============================================================================

class AdoClient:
    def __init__(self, base_url: str, pat: str):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.auth = ("", pat)
        self.session.headers.update({"Content-Type": "application/json"})

    def get_active_cards(self) -> List[Dict[str, Any]]:
        """用 WIQL 查出專案中所有未結案卡片，並抓出欄位與 Tags"""
        wiql_url = f"{self.base_url}/wit/wiql"
        payload = {
            "query": (
                "SELECT [System.Id] FROM WorkItems "
                f"WHERE [System.TeamProject] = '{ADO_PROJECT}' "
                "  AND [System.State] NOT IN ('Closed', 'Done', 'Removed')"
            )
        }
        resp = self.session.post(wiql_url, params={"api-version": "7.1"}, json=payload)
        resp.raise_for_status()

        refs = resp.json().get("workItems", [])
        if not refs:
            return []

        ids = [item["id"] for item in refs]
        fields = ["System.Id", "System.Title", "System.BoardColumn", "System.Tags", "System.WorkItemType"]

        # 分頁批次取得（每 100 筆一次）
        cards = []
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            batch_url = f"{self.base_url}/wit/workitems"
            batch_resp = self.session.get(
                batch_url,
                params={"ids": ",".join(map(str, chunk)), "fields": ",".join(fields), "api-version": "7.1"},
            )
            batch_resp.raise_for_status()
            for item in batch_resp.json().get("value", []):
                f = item.get("fields", {})
                raw_tags = f.get("System.Tags", "")
                tags_list = [t.strip() for t in raw_tags.split(";") if t.strip()] if raw_tags else []
                cards.append({
                    "id": f.get("System.Id"),
                    "title": f.get("System.Title", ""),
                    "column": f.get("System.BoardColumn", ""),
                    "type": f.get("System.WorkItemType", ""),
                    "tags": tags_list,
                })
        return cards

    def add_tag(self, work_item_id: int, new_tag: str):
        current_tags = self._get_tags(work_item_id)
        if new_tag not in current_tags:
            current_tags.append(new_tag)
            self._save_tags(work_item_id, current_tags)

    def remove_tag(self, work_item_id: int, tag_to_remove: str):
        current_tags = self._get_tags(work_item_id)
        if tag_to_remove in current_tags:
            current_tags = [t for t in current_tags if t != tag_to_remove]
            self._save_tags(work_item_id, current_tags)

    def _get_tags(self, work_item_id: int) -> List[str]:
        url = f"{self.base_url}/wit/workitems/{work_item_id}"
        resp = self.session.get(url, params={"$fields": "System.Tags", "api-version": "7.1"})
        resp.raise_for_status()
        raw = resp.json().get("fields", {}).get("System.Tags", "")
        return [t.strip() for t in raw.split(";") if t.strip()] if raw else []

    def _save_tags(self, work_item_id: int, tags: List[str]):
        url = f"{self.base_url}/wit/workitems/{work_item_id}"
        body = [{"op": "add", "path": "/fields/System.Tags", "value": "; ".join(tags)}]
        resp = self.session.patch(
            url,
            params={"api-version": "7.1"},
            json=body,
            headers={"Content-Type": "application/json-patch+json"},
        )
        resp.raise_for_status()


# =============================================================================
# 3. GitLab CI 派工
# =============================================================================

def trigger_gitlab_agent(agent_name: str, work_item_id: int) -> bool:
    """呼叫 GitLab CI Pipeline Trigger API 啟動 Agent"""
    url = f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT_ID}/pipeline"
    branch_name = f"feat/wi-{work_item_id}"
    variables = [
        {"key": "ADO_ORGANIZATION", "value": ADO_ORG},
        {"key": "ADO_PROJECT", "value": ADO_PROJECT},
        {"key": "WORK_ITEM_ID", "value": str(work_item_id)},
        {"key": "AGENT_NAME", "value": agent_name},
        {"key": "TARGET_BRANCH", "value": branch_name},
    ]
    if TARGET_REPO_URL:
        variables.append({"key": "TARGET_REPO_URL", "value": TARGET_REPO_URL})

    payload = {
        "ref": GITLAB_DEFAULT_REF,
        "variables": variables,
    }
    try:
        resp = requests.post(url, headers={"PRIVATE-TOKEN": GITLAB_TOKEN}, json=payload, timeout=15)
        resp.raise_for_status()
        log.info(f"     ✅ GitLab CI Pipeline #{resp.json().get('id')} 已啟動 ({agent_name}) -> 分支: {branch_name}")
        return True
    except Exception as e:
        log.error(f"     ❌ 啟動 GitLab CI 失敗 ({agent_name}): {e}")
        return False


# =============================================================================
# 4. Orchestrator 主迴圈 (極簡狀態調和)
# =============================================================================

def main():
    log.info("🤖 ADO AI Bot Orchestrator 啟動 [極簡調和模式]...")
    ado = AdoClient(ADO_BASE_URL, ADO_PAT)

    try:
        cards = ado.get_active_cards()
    except Exception as e:
        log.critical(f"無法查詢 ADO 卡片: {e}")
        sys.exit(1)

    log.info(f"掃描到 {len(cards)} 張活躍卡片，開始評估...\n")
    dispatched = 0

    for card in cards:
        column = card["column"]
        if column not in STAGE_MAPPING:
            continue

        agent_name = STAGE_MAPPING[column]
        done_tag = f"{agent_name}:done"
        running_tag = f"{agent_name}:running"

        # 核心判定：只要該 Agent「沒做過」且「沒有人在跑中」，就派工！
        # （人類如果想原地重做，只要把 {agent_name}:done 點掉即可！）
        if done_tag not in card["tags"] and running_tag not in card["tags"]:
            log.info(f"[WI #{card['id']}] 🎯 欄位 '{column}' 需要執行 {agent_name}...")

            # 1. 打上 running 標籤防止重複派工
            ado.add_tag(card["id"], running_tag)

            # 2. 觸發 GitLab CI
            success = trigger_gitlab_agent(agent_name, card["id"])

            # 3. 若失敗則回滾移除 running
            if not success:
                ado.remove_tag(card["id"], running_tag)
            else:
                dispatched += 1

    log.info(f"\n🎉 巡邏完成，共派工 {dispatched} 個任務。")


if __name__ == "__main__":
    main()
