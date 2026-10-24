"""
ADO AI Bot Framework — Base Agent (Docs-as-Code / Repo-Centric 版)
================================================================
【核心職責】：
1. 讀取 Work Item 上下文（標題、描述、既有留言，以及本地 Git Repo 既有文件）
2. 執行 AI 任務 (execute)
3. 成果直接寫入本地 Git Repo（docs/features/wi-{id}/... 或 src/...）
4. 自動提交 Git Commit 並推播 (Push) 至功能分支 feat/wi-{id}
5. 發布 ADO Discussion 留言（精簡摘要、待確認清單、Git Commit/Diff 連結）
6. 狀態更新：移除 {stage}:running，加上 {stage}:done
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
            # 注入 token 進行認證
            auth_url = self.target_repo_url.replace("https://", f"https://oauth2:{self.git_token}@")

        log.info(f"正在 Clone 目標業務儲存庫至 {self.workspace_dir}...")
        subprocess.run(["git", "clone", auth_url, str(self.workspace_dir)], check=True)

        # 設定使用者與切換分支
        subprocess.run(["git", "-C", str(self.workspace_dir), "config", "user.name", "AI SDLC Bot"], check=True)
        subprocess.run(["git", "-C", str(self.workspace_dir), "config", "user.email", "ai-bot@company.com"], check=True)

        # 檢查遠端是否存在該分支
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
            # 加入檔案
            for f in files:
                subprocess.run(["git", "-C", repo_cwd, "add", f], check=True)

            # 檢查是否有變更
            status_proc = subprocess.run(["git", "-C", repo_cwd, "diff", "--cached", "--quiet"], capture_output=True)
            if status_proc.returncode == 0:
                log.info("目標儲存庫無檔案變更，略過 commit。")
                commit_hash = subprocess.run(["git", "-C", repo_cwd, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
                return commit_hash

            commit_title = f"🤖 [{self.display_name}] {summary_msg} (WI #{self.work_item_id})"
            subprocess.run(["git", "-C", repo_cwd, "commit", "-m", commit_title], check=True)

            commit_hash = subprocess.run(["git", "-C", repo_cwd, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            log.info(f"目標儲存庫 Git commit 完成: {commit_hash[:8]} - {commit_title}")

            # 推播至遠端目標分支
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

        # 待確認事項
        items_md = ""
        if result.action_items_for_human:
            items = "\n".join([f"- {item}" for item in result.action_items_for_human])
            items_md = f"### 待確認事項\n\n{items}\n\n"

        # 產出檔案清單 (Docs-as-Code)
        files_md = ""
        if result.files_written:
            files_list = "\n".join([f"- `{f}`" for f in result.files_written])
            files_md = f"### 本地儲存庫產出檔案 (Docs-as-Code)\n\n{files_list}\n\n"

        # Git 來源與 Commit 連結
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
            # 1. 準備目標業務 Repo 工作目錄（若有配置 TARGET_REPO_URL）
            self._prepare_target_repo()

            work_item = self._get_work_item()
            comments = self._get_comments()

            result = self.execute(work_item, comments)

            # 2. 若有檔案需要 commit 到目標業務 Repo
            commit_hash = None
            if result.files_written:
                commit_hash = self._commit_and_push(result.files_written, f"產出 {self.stage} 規格")

            # 3. 備用：若仍有附件要上傳至 ADO
            for att in result.attachments:
                content = att["content"]
                if isinstance(content, str):
                    content = content.encode("utf-8")
                self._upload_attachment(att["filename"], content)

            # 4. 發布 ADO Discussion 摘要
            self._post_discussion(result, commit_hash=commit_hash)

            # 5. 更新標籤為 {stage}:done
            self._finish_tags()
            log.info(f"=== {self.display_name} 完成 ===")

        except Exception as e:
            log.exception(f"執行出錯: {e}")
            sys.exit(1)
