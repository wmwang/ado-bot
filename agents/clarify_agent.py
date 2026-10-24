"""
ADO AI Bot Framework — Clarify Agent 實作範例
=============================================
對應 SDLC 藍圖中的 Clarify 階段（R1 規則）：
1. 觸發條件：Work Item 移至 Clarify 看板欄位
2. 任務：
   - 審視 PM 填寫的標題、描述及既有 Discussion
   - 檢查需求是否具備清晰的「驗收標準 (Acceptance Criteria)」、「影響範圍」與「非功能需求」
   - 產出結構化的「需求釐清分析報告」並提出追問清單
3. 輸出：
   - Discussion：精簡報告與問題清單
   - Attachment：完整的需求釐清與審核規格報告 (.md)
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, Any, List
try:
    from base_agent import BaseAgent, AgentResult
except ImportError:
    from agents.base_agent import BaseAgent, AgentResult

log = logging.getLogger("clarify_agent")


class ClarifyAgent(BaseAgent):
    """需求釐清與完整性分析 Agent"""

    @property
    def agent_name(self) -> str:
        return "clarify-agent"

    @property
    def display_name(self) -> str:
        return "需求釐清 Agent (Clarify)"

    def _call_llm_for_clarification(self, title: str, description: str, comments_text: str) -> Dict[str, Any]:
        """
        呼叫大型語言模型 (LLM) 進行需求分析。
        在 prototype 階段，這裡示範結構化 Prompt 與預設回覆格式；
        正式環境可替換為 OpenAI / Azure OpenAI / Anthropic / Gemini SDK 呼叫。
        """
        api_key = os.environ.get("LLM_API_KEY")
        if api_key:
            log.info("偵測到 LLM_API_KEY，可連線至指定 LLM 服務...")
            # 此處可直接串接例如 openai.chat.completions.create(...)
            pass

        # 模擬 LLM 分析回傳結果
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        simulated_report = f"""# 需求完整性評估報告 (Clarify)

- **目標 Work Item：** #{self.work_item_id} - {title}
- **評估時間：** {timestamp}
- **評估面向：**
  1. **業務目標清晰度：** 8/10 — 目標大致明確，但缺乏預期成功量化指標。
  2. **驗收條件 (AC) 完備度：** 5/10 — 尚未定義邊界條件與例外狀況。
  3. **非功能需求定義：** 3/10 — 未提及回應時間、預期 QPS 與資安授權規格。

---

## 建議補齊的驗收標準草案 (Given-When-Then)
- **情境 1：標準流程**
  - Given: 使用者已通過身分驗證
  - When: 提交資料請求
  - Then: 系統於 1 秒內回傳格式化結果
- **情境 2：例外流程**
  - Given: 輸入欄位包含無效參數
  - When: 提交請求
  - Then: 系統回傳 HTTP 400 及對應錯誤代碼
"""

        simulated_questions = [
            "請問此功能是否有特定的 QPS (每秒請求數) 或預期尖峰負載限制？",
            "當驗證失敗或外部服務逾時時，系統預期的回退機制 (Fallback) 為何？",
            "此功能是否需要配合稽核日誌 (Audit Log) 保存特定操作紀錄？",
        ]

        summary_text = (
            f"已完成對 **#{self.work_item_id} {title}** 的需求完整度初審。\n\n"
            "- **業務目標清晰度**：良好，已涵蓋基礎情境\n"
            "- **潛在風險**：缺乏非功能需求（性能指標、例外處置原則）\n"
            "- **已產出**：已自動生成驗收條件 (AC) 草案與完整評估報告，詳見附件。"
        )

        return {
            "summary": summary_text,
            "questions": simulated_questions,
            "detailed_report": simulated_report,
        }

    def execute(self, work_item: Dict[str, Any], comments: List[Dict[str, Any]]) -> AgentResult:
        """執行需求釐清分析"""
        fields = work_item.get("fields", {})
        title = fields.get("System.Title", "未命名需求")
        description = fields.get("System.Description", "(無描述)")

        # 彙整過去留言紀錄
        parsed_comments = []
        for c in comments:
            author = c.get("createdBy", {}).get("displayName", "未知使用者")
            text = c.get("text", "")
            # 過濾掉 Bot 自行發布的留言
            if not text.startswith("## 🤖"):
                parsed_comments.append(f"[{author}]: {text}")
        comments_combined = "\n".join(parsed_comments)

        log.info(f"正在對 Work Item '{title}' 進行需求分析...")
        llm_output = self._call_llm_for_clarification(title, description, comments_combined)

        today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        
        # 1. Docs-as-Code: 直接寫入本地儲存庫的 feature 專屬目錄
        # 路徑：docs/features/wi-{id}/01_clarify.md
        written_path = self.write_feature_file("01_clarify.md", llm_output["detailed_report"])

        return AgentResult(
            summary=llm_output["summary"],
            action_items_for_human=llm_output["questions"],
            files_written=[written_path],
        )


if __name__ == "__main__":
    agent = ClarifyAgent()
    agent.run()
