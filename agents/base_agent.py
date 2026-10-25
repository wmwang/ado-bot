"""
ADO AI Bot Framework — Base Agent & Universal OpenCode Runner (Docs-as-Code 版)
================================================================================
【核心職責】：
1. 動態 Clone 目標業務儲存庫至 /workspace 並切換至功能分支 feat/wi-{id}
2. 讀取 Work Item 上下文（標題、描述、既有留言）
3. 透過 OpenCode CLI 執行「生成 (Writer) -> 審查 (Reviewer) -> 修正 (Refine)」品管迴圈
   （內部由 opencode.json 自動調度 @explore 唯讀子代理探索程式碼庫）
4. 自動偵測 /workspace 變更檔案，提交 Git Commit 並 Push 至 feat/wi-{id}
5. 發布 ADO Discussion 留言（精簡摘要、待確認清單、Git Commit/Diff 連結）
6. 狀態更新：移除 {agent_name}:running，加上 {agent_name}:done
"""

import logging
import os
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("base_agent")

# 定位 ado-bot 根目錄下的 opencode.json 設定檔
BOT_ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OPENCODE_CONFIG = BOT_ROOT_DIR / "opencode.json"
MAX_REVIEW_ROUNDS = int(os.environ.get("MAX_REVIEW_ROUNDS", "2"))


@dataclass
class AgentResult:
    summary: str
    action_items_for_human: List[str] = field(default_factory=list)
    files_written: List[str] = field(default_factory=list)  # 本地 Repo 產出檔案路徑清單
    attachments: List[Dict[str, Any]] = field(default_factory=list)  # 備用：若仍需上傳 ADO Attachment


class BaseAgent(ABC):
    def __init__(self):
        self.ado_org = os.environ.get("ADO_ORGANIZATION", "")
        self.ado_project = os.environ.get("ADO_PROJECT", "")
        self.ado_pat = os.environ.get("ADO_PAT", "")
        self.work_item_id = int(os.environ.get("WORK_ITEM_ID", "0"))
        self.target_branch = os.environ.get("TARGET_BRANCH", f"feat/wi-{self.work_item_id}")

        # 目標業務 Repo 設定（解耦：工具 Repo vs 目標業務 Repo）
        self.target_repo_url = os.environ.get("TARGET_REPO_URL", "")
        self.git_token = os.environ.get("GIT_TOKEN", os.environ.get("GITLAB_TOKEN", ""))
        self.gitlab_project_url = os.environ.get("TARGET_PROJECT_URL", "")  # 目標專案的 Web UI 網址

        if not self.ado_pat or not self.work_item_id:
            log.error("缺少必要環境變數 (ADO_PAT 或 WORK_ITEM_ID)")
            sys.exit(1)

        self.base_url = f"https://dev.azure.com/{self.ado_org}/{self.ado_project}/_apis"
        self.session = requests.Session()
        self.session.auth = ("", self.ado_pat)

        # 決定工作目錄：若指定 TARGET_REPO_URL，則在 workspace/ 下操作目標 Repo
        if self.target_repo_url:
            self.workspace_dir = Path("workspace")
        else:
            self.workspace_dir = Path(".")

        # 規範專屬文件目錄：<workspace>/docs/features/wi-{work_item_id}
        self.feature_dir = self.workspace_dir / "docs" / "features" / f"wi-{self.work_item_id}"

    @property
    @abstractmethod
    def agent_name(self) -> str:
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        pass

    @abstractmethod
    def execute(self, work_item: Dict[str, Any], comments: List[Dict[str, Any]]) -> AgentResult:
        pass

    # =========================================================================
    # 目標業務儲存庫準備 (Clone & Checkout Target Repo)
    # =========================================================================

    def _prepare_target_repo(self):
        """將目標業務儲存庫 clone 到 workspace 並切換至專屬功能分支"""
        if not self.target_repo_url:
            log.info("未指定 TARGET_REPO_URL，使用當前本地環境操作。")
            return

        if self.workspace_dir.exists():
            log.info(f"清理既有 workspace: {self.workspace_dir}")
            subprocess.run(["rm", "-rf", str(self.workspace_dir)], check=True)

        auth_url = self.target_repo_url
        if self.git_token and "https://" in self.target_repo_url:
            auth_url = self.target_repo_url.replace("https://", f"https://oauth2:{self.git_token}@")

        log.info(f"正在 Clone 目標業務儲存庫至 {self.workspace_dir}...")
        subprocess.run(["git", "clone", auth_url, str(self.workspace_dir)], check=True)

        subprocess.run(["git", "-C", str(self.workspace_dir), "config", "user.name", "AI SDLC Bot"], check=True)
        subprocess.run(["git", "-C", str(self.workspace_dir), "config", "user.email", "ai-bot@company.com"], check=True)

        fetch_res = subprocess.run(
            ["git", "-C", str(self.workspace_dir), "fetch", "origin", self.target_branch],
            capture_output=True,
        )
        if fetch_res.returncode == 0:
            log.info(f"切換至既有分支: {self.target_branch}")
            subprocess.run(["git", "-C", str(self.workspace_dir), "checkout", self.target_branch], check=True)
        else:
            log.info(f"建立新分支: {self.target_branch}")
            subprocess.run(["git", "-C", str(self.workspace_dir), "checkout", "-b", self.target_branch], check=True)

    # =========================================================================
    # OpenCode CLI 三明治品管迴圈 (Writer -> Reviewer -> Refine)
    # =========================================================================

    def _invoke_opencode(self, oc_agent: str, prompt_text: str) -> str:
        """在目標業務儲存庫 (/workspace) 內呼叫 opencode run，並掛載 ado-bot 的 opencode.json"""
        env = os.environ.copy()
        env["OPENCODE_CONFIG"] = str(DEFAULT_OPENCODE_CONFIG)

        cmd = [
            "opencode",
            "run",
            "--auto",
            "--agent",
            oc_agent,
            prompt_text,
        ]
        log.info(f"🚀 執行 OpenCode Agent [{oc_agent}] (cwd={self.workspace_dir})...")
        proc = subprocess.run(
            cmd,
            cwd=str(self.workspace_dir),
            env=env,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            log.error(f"OpenCode [{oc_agent}] 執行失敗:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}")
            raise RuntimeError(f"OpenCode {oc_agent} exited with code {proc.returncode}")
        return proc.stdout.strip()

    def run_opencode_with_review(self, work_item: Dict[str, Any], comments: List[Dict[str, Any]]) -> AgentResult:
        """
        標準三明治協作迴圈：
        Step 1 (Draft): 呼叫 {agent_name}-writer（內部可自主呼叫 @explore 探索程式碼庫並產出初稿）
        Step 2 (Review): 呼叫 {agent_name}-reviewer（全新獨立 Context 唯讀審查，輸出 [PASS] 或 [REJECT]）
        Step 3 (Refine): 若為 [REJECT]，將審查意見餵回 {agent_name}-writer 修正（最多 MAX_REVIEW_ROUNDS 輪）
        """
        fields = work_item.get("fields", {})
        title = fields.get("System.Title", "")
        description = fields.get("System.Description", "") or "(無描述)"
        recent_comments = "\n".join(
            [f"- [{c.get('createdDate', '')}] {c.get('text', '')}" for c in comments[-10:]]
        ) or "(無歷史留言)"

        writer_agent = f"{self.agent_name}-writer"
        reviewer_agent = f"{self.agent_name}-reviewer"

        base_context = (
            f"【Work Item ID】: {self.work_item_id}\n"
            f"【功能專屬規格目錄】: docs/features/wi-{self.work_item_id}/\n"
            f"【卡片標題】: {title}\n"
            f"【卡片描述】:\n{description}\n\n"
            f"【近期討論串回覆】:\n{recent_comments}\n"
        )

        # Step 1: 撰寫初稿 (Draft)
        log.info(f"--- [Step 1: Draft] 啟動 {writer_agent} ---")
        writer_summary = self._invoke_opencode(
            writer_agent,
            f"請針對以下 Work Item 執行你的階段任務（注意將 {{WORK_ITEM_ID}} 替換為 {self.work_item_id}）：\n\n{base_context}",
        )

        # Step 2 & Step 3: 獨立審查與修正迴圈 (Review <-> Refine)
        for round_idx in range(1, MAX_REVIEW_ROUNDS + 1):
            log.info(f"--- [Step 2: Review (第 {round_idx}/{MAX_REVIEW_ROUNDS} 輪)] 啟動 {reviewer_agent} ---")
            review_output = self._invoke_opencode(
                reviewer_agent,
                f"請審查剛剛針對 Work Item #{self.work_item_id} 產出的檔案與變更。以下為卡片背景：\n\n{base_context}",
            )

            if "[PASS]" in review_output:
                log.info(f"✅ {reviewer_agent} 審查通過 ([PASS])！")
                break

            log.warning(f"⚠️ {reviewer_agent} 提出修正要求 ([REJECT]):\n{review_output}")
            log.info(f"--- [Step 3: Refine (第 {round_idx}/{MAX_REVIEW_ROUNDS} 輪)] 啟動 {writer_agent} 進行修正 ---")
            writer_summary = self._invoke_opencode(
                writer_agent,
                (
                    f"審查員 ({reviewer_agent}) 針對你對 Work Item #{self.work_item_id} 的產出提出了以下修正要求，"
                    f"請立即修正對應檔案並更新最終摘要：\n\n"
                    f"【審查意見】:\n{review_output}\n\n"
                    f"【原始卡片背景】:\n{base_context}"
                ),
            )

        changed_files = self._detect_changed_files()
        return AgentResult(
            summary=writer_summary or f"已完成 `{self.agent_name}` 階段任務並通過獨立審查。",
            files_written=changed_files,
        )

    def _detect_changed_files(self) -> List[str]:
        """透過 git status --porcelain 自動偵測 /workspace 內所有新增或修改的檔案"""
        repo_cwd = str(self.workspace_dir)
        try:
            proc = subprocess.run(
                ["git", "-C", repo_cwd, "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            )
            files = []
            for line in proc.stdout.splitlines():
                if not line.strip():
                    continue
                # porcelain 格式前 3 字元為狀態碼，之後為檔案路徑
                path_str = line[3:].strip()
                files.append(path_str)
            return files
        except Exception:
            return []

    # =========================================================================
    # 本地檔案管理 (Docs-as-Code Context in Target Repo)
    # =========================================================================

    def write_feature_file(self, filename: str, content: str) -> str:
        """寫入目標 Repo 的專屬規格檔案 (docs/features/wi-{id}/<filename>)"""
        self.feature_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.feature_dir / filename
        target_path.write_text(content, encoding="utf-8")
        rel_path = target_path.relative_to(self.workspace_dir)
        log.info(f"目標儲存庫規格檔案已寫入: {rel_path}")
        return str(rel_path)

    def read_feature_file(self, filename: str) -> Optional[str]:
        """讀取目標 Repo 的專屬規格檔案"""
        target_path = self.feature_dir / filename
        if target_path.exists():
            return target_path.read_text(encoding="utf-8")
        return None

    def feature_file_exists(self, filename: str) -> bool:
        """檢查目標 Repo 的規格檔案是否存在"""
        return (self.feature_dir / filename).exists()

    # =========================================================================
    # Git 操作 (作用於目標業務儲存庫)
    # =========================================================================

    def _commit_and_push(self, files: List[str], summary_msg: str) -> Optional[str]:
        """將產出的檔案提交並推播至目標業務儲存庫"""
        repo_cwd = str(self.workspace_dir)
        try:
            subprocess.run(["git", "-C", repo_cwd, "rev-parse", "--is-inside-work-tree"], check=True, capture_output=True)
        except Exception:
            log.warning("目標目錄非 Git 儲存庫，略過 git commit/push 步驟（本地測試模式）。")
            return None

        try:
            for f in files:
                subprocess.run(["git", "-C", repo_cwd, "add", f], check=True)

            status_proc = subprocess.run(["git", "-C", repo_cwd, "diff", "--cached", "--quiet"], capture_output=True)
            if status_proc.returncode == 0:
                log.info("目標儲存庫無檔案變更，略過 commit。")
                commit_hash = subprocess.run(["git", "-C", repo_cwd, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
                return commit_hash

            commit_title = f"🤖 [{self.display_name}] {summary_msg} (WI #{self.work_item_id})"
            subprocess.run(["git", "-C", repo_cwd, "commit", "-m", commit_title], check=True)

            commit_hash = subprocess.run(["git", "-C", repo_cwd, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            log.info(f"目標儲存庫 Git commit 完成: {commit_hash[:8]} - {commit_title}")

            push_proc = subprocess.run(["git", "-C", repo_cwd, "push", "origin", f"HEAD:{self.target_branch}"], capture_output=True, text=True)
            if push_proc.returncode == 0:
                log.info(f"已成功推播至目標儲存庫分支: {self.target_branch}")
            else:
                log.warning(f"Git push 警告: {push_proc.stderr}")

            return commit_hash
        except Exception as e:
            log.error(f"目標儲存庫 Git 操作失敗: {e}")
            return None

    # =========================================================================
    # ADO API 溝通
    # =========================================================================

    def _get_work_item(self) -> Dict[str, Any]:
        url = f"{self.base_url}/wit/workitems/{self.work_item_id}"
        resp = self.session.get(url, params={"$expand": "all", "api-version": "7.1"})
        resp.raise_for_status()
        return resp.json()

    def _get_comments(self) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/wit/workItems/{self.work_item_id}/comments"
        resp = self.session.get(url, params={"api-version": "7.1-preview.4", "$top": 50, "order": "asc"})
        if resp.status_code == 200:
            return resp.json().get("comments", [])
        return []

    def _upload_attachment(self, filename: str, content_bytes: bytes):
        upload_resp = self.session.post(
            f"{self.base_url}/wit/attachments",
            params={"fileName": filename, "api-version": "7.1"},
            data=content_bytes,
            headers={"Content-Type": "application/octet-stream"},
        )
        upload_resp.raise_for_status()
        att_url = upload_resp.json()["url"]

        self.session.patch(
            f"{self.base_url}/wit/workitems/{self.work_item_id}",
            params={"api-version": "7.1"},
            json=[{
                "op": "add",
                "path": "/relations/-",
                "value": {"rel": "AttachedFile", "url": att_url},
            }],
            headers={"Content-Type": "application/json-patch+json"},
        ).raise_for_status()

    def _post_discussion(self, result: AgentResult, commit_hash: Optional[str] = None):
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        items_md = ""
        if result.action_items_for_human:
            items = "\n".join([f"- {item}" for item in result.action_items_for_human])
            items_md = f"### 待確認事項\n\n{items}\n\n"

        files_md = ""
        if result.files_written:
            files_list = "\n".join([f"- `{f}`" for f in result.files_written])
            files_md = f"### 本地儲存庫產出檔案 (Docs-as-Code)\n\n{files_list}\n\n"

        git_info_md = ""
        if commit_hash:
            short_sha = commit_hash[:8]
            if self.gitlab_project_url:
                commit_link = f"[{short_sha}]({self.gitlab_project_url}/-/commit/{commit_hash})"
                branch_link = f"[{self.target_branch}]({self.gitlab_project_url}/-/tree/{self.target_branch})"
                git_info_md = f"**Git 分支：** {branch_link} | **Commit：** {commit_link}\n\n"
            else:
                git_info_md = f"**Git 分支：** `{self.target_branch}` | **Commit：** `{short_sha}`\n\n"

        text = f"""## 🤖 {self.display_name} 報告 ({now_utc})

{result.summary}

{git_info_md}{files_md}{items_md}---
*若產出符合預期，請拖曳卡片推進；若需重做，請直接點擊卡片標籤上的「✕」移除 `{self.agent_name}:done`。*
"""
        self.session.post(
            f"{self.base_url}/wit/workItems/{self.work_item_id}/comments",
            params={"api-version": "7.1-preview.4"},
            json={"text": text},
        ).raise_for_status()

    def _finish_tags(self):
        """完工標籤切換：移除 {agent_name}:running，加上 {agent_name}:done"""
        url = f"{self.base_url}/wit/workitems/{self.work_item_id}"
        resp = self.session.get(url, params={"$fields": "System.Tags", "api-version": "7.1"})
        raw_tags = resp.json().get("fields", {}).get("System.Tags", "")
        current = [t.strip() for t in raw_tags.split(";") if t.strip()] if raw_tags else []

        running_tag = f"{self.agent_name}:running"
        done_tag = f"{self.agent_name}:done"

        cleaned = [t for t in current if t != running_tag]
        if done_tag not in cleaned:
            cleaned.append(done_tag)

        self.session.patch(
            url,
            params={"api-version": "7.1"},
            json=[{"op": "add", "path": "/fields/System.Tags", "value": "; ".join(cleaned)}],
            headers={"Content-Type": "application/json-patch+json"},
        ).raise_for_status()

    def run(self):
        log.info(f"=== {self.display_name} 啟動，處理 WI #{self.work_item_id} ===")
        try:
            self._prepare_target_repo()

            work_item = self._get_work_item()
            comments = self._get_comments()

            result = self.execute(work_item, comments)

            commit_hash = None
            if result.files_written:
                commit_hash = self._commit_and_push(result.files_written, f"產出 {self.agent_name} 階段成果")

            for att in result.attachments:
                content = att["content"]
                if isinstance(content, str):
                    content = content.encode("utf-8")
                self._upload_attachment(att["filename"], content)

            self._post_discussion(result, commit_hash=commit_hash)
            self._finish_tags()
            log.info(f"=== {self.display_name} 完成 ===")

        except Exception as e:
            log.exception(f"執行出錯: {e}")
            sys.exit(1)


# =============================================================================
# 通用配置驅動執行器 (Universal OpenCode Agent)
# 團隊僅需在 opencode.json 與 prompts/ 定義行為，無需為每個階段撰寫 Python 子類別！
# =============================================================================

STAGE_DISPLAY_NAMES = {
    "clarify-agent": "需求釐清 Agent (Clarify)",
    "requirement-agent": "需求規格 Agent (Requirement)",
    "contract-agent": "介面契約 Agent (Contract)",
    "scaffold-agent": "程式碼實作 Agent (Scaffold)",
    "test-gen-agent": "自動化測試 Agent (TestGen)",
}


class UniversalOpenCodeAgent(BaseAgent):
    """直接根據環境變數 AGENT_NAME 讀取 opencode.json 執行 Writer -> Reviewer 迴圈"""

    def __init__(self, agent_name_override: Optional[str] = None):
        super().__init__()
        self._agent_name = agent_name_override or os.environ.get("AGENT_NAME", "clarify-agent")

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def display_name(self) -> str:
        return STAGE_DISPLAY_NAMES.get(self._agent_name, f"AI Agent ({self._agent_name})")

    def execute(self, work_item: Dict[str, Any], comments: List[Dict[str, Any]]) -> AgentResult:
        return self.run_opencode_with_review(work_item, comments)


if __name__ == "__main__":
    UniversalOpenCodeAgent().run()
