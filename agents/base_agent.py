"""
ADO AI Bot — 極簡 Container Runner
==================================
【設計原則】：
1. 流程 100% 交給 `.opencode/` 內的 Markdown Agents 彈性自治，本腳本不寫死任何多步審查迴圈。
2. 每個 SDLC 階段永遠只呼叫唯一一行 `opencode run --dir /workspace --agent <stage> -f /tmp/wi-{id}.md`。
3. 針對 `dev` 階段，在 Git Push 前執行最後一道確定性出口驗收（Maven/Gradle Test Exit Code == 0）。
"""

import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ado_runner")

BOT_ROOT_DIR = Path(__file__).resolve().parent.parent
OPENCODE_CONFIG_DIR = BOT_ROOT_DIR / ".opencode"

STAGE_TITLES = {
    "clarify": "需求釐清 Agent (Clarify)",
    "spec": "規格與設計 Agent (Spec)",
    "dev": "研發交付 Agent (Dev)",
}


class AdoOpenCodeRunner:
    def __init__(self):
        self.ado_org = os.environ.get("ADO_ORGANIZATION", "")
        self.ado_project = os.environ.get("ADO_PROJECT", "")
        self.ado_pat = os.environ.get("ADO_PAT", "")
        self.work_item_id = int(os.environ.get("WORK_ITEM_ID", "0"))
        # 相容傳入 clarify / clarify-agent 等格式，統一正規化為 .opencode/agents/<stage>.md 名稱
        raw_stage = os.environ.get("AGENT_NAME", "clarify").removesuffix("-agent")
        self.stage = raw_stage
        self.tag_prefix = os.environ.get("AGENT_NAME", self.stage)

        self.target_branch = os.environ.get("TARGET_BRANCH", f"feat/wi-{self.work_item_id}")
        self.target_repo_url = os.environ.get("TARGET_REPO_URL", "")
        self.git_token = os.environ.get("GIT_TOKEN", os.environ.get("GITLAB_TOKEN", ""))
        self.project_web_url = os.environ.get("TARGET_PROJECT_URL", "")
        self.verify_cmd_override = os.environ.get("VERIFY_CMD", "")

        if not self.ado_pat or not self.work_item_id:
            log.error("缺少必要環境變數：ADO_PAT 或 WORK_ITEM_ID")
            sys.exit(1)

        self.base_url = f"https://dev.azure.com/{self.ado_org}/{self.ado_project}/_apis"
        self.session = requests.Session()
        self.session.auth = ("", self.ado_pat)
        self.workspace_dir = Path("workspace") if self.target_repo_url else Path(".")

    @property
    def display_name(self) -> str:
        return STAGE_TITLES.get(self.stage, f"AI Agent ({self.stage})")

    def prepare_repo(self):
        """在 Container 內 Clone 目標業務儲存庫並切換至功能分支"""
        if not self.target_repo_url:
            log.info("未指定 TARGET_REPO_URL，使用當前目錄執行。")
            return

        if self.workspace_dir.exists():
            subprocess.run(["rm", "-rf", str(self.workspace_dir)], check=True)

        auth_url = self.target_repo_url
        if self.git_token and "https://" in self.target_repo_url:
            auth_url = self.target_repo_url.replace("https://", f"https://oauth2:{self.git_token}@")

        log.info(f"Clone 目標業務儲存庫至 {self.workspace_dir}...")
        subprocess.run(["git", "clone", auth_url, str(self.workspace_dir)], check=True)
        subprocess.run(["git", "-C", str(self.workspace_dir), "config", "user.name", "AI SDLC Bot"], check=True)
        subprocess.run(["git", "-C", str(self.workspace_dir), "config", "user.email", "ai-bot@company.com"], check=True)

        fetch_res = subprocess.run(
            ["git", "-C", str(self.workspace_dir), "fetch", "origin", self.target_branch],
            capture_output=True,
        )
        checkout_cmd = (
            ["git", "-C", str(self.workspace_dir), "checkout", self.target_branch]
            if fetch_res.returncode == 0
            else ["git", "-C", str(self.workspace_dir), "checkout", "-b", self.target_branch]
        )
        subprocess.run(checkout_cmd, check=True)

    def build_context_file(self, work_item: Dict[str, Any], comments: List[Dict[str, Any]]) -> Path:
        """將 ADO Work Item 內容寫入暫存 Markdown 檔，透過 -f 乾淨傳入 OpenCode"""
        fields = work_item.get("fields", {})
        wi_type = fields.get("System.WorkItemType", "Work Item")
        title = fields.get("System.Title", "")
        description = fields.get("System.Description", "") or "(無描述)"
        ac_field = fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "")

        human_comments = [
            f"- [{c.get('createdDate', '')}] {c.get('createdBy', {}).get('displayName', '')}: {c.get('text', '')}"
            for c in comments[-15:]
            if not c.get("text", "").startswith("## 🤖")
        ]
        comments_md = "\n".join(human_comments) or "(尚無人類討論留言)"

        content = (
            f"# Work Item #{self.work_item_id} [{wi_type}] — {title}\n\n"
            f"- **功能規格目錄**：`docs/features/wi-{self.work_item_id}/`\n"
            f"- **目標分支**：`{self.target_branch}`\n\n"
            f"## 卡片描述\n{description}\n\n"
        )
        if ac_field:
            content += f"## 卡片原始驗收條件欄位\n{ac_field}\n\n"
        content += f"## 討論區歷史回覆\n{comments_md}\n"

        ctx_path = Path(tempfile.gettempdir()) / f"wi-{self.work_item_id}-context.md"
        ctx_path.write_text(content, encoding="utf-8")
        return ctx_path

    def run_opencode(self, context_file: Path) -> str:
        """全階段統一的單行 OpenCode 呼叫入口"""
        env = {
            **os.environ,
            "OPENCODE_CONFIG_DIR": str(OPENCODE_CONFIG_DIR),
            "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
        }
        prompt = (
            f"請針對附件的 Work Item #{self.work_item_id} 完成 `{self.stage}` 階段交付 "
            f"（注意將規格路徑中的 {{WORK_ITEM_ID}} 替換為 {self.work_item_id}）。"
        )
        cmd = [
            "opencode",
            "run",
            "--dir",
            str(self.workspace_dir.resolve()),
            "--agent",
            self.stage,
            "--dangerously-skip-permissions",
            "-f",
            str(context_file),
            prompt,
        ]
        log.info(f"🚀 啟動 OpenCode Agent [{self.stage}]...")
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            log.error(f"OpenCode 執行異常:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
            raise RuntimeError(f"OpenCode agent '{self.stage}' 失敗 (exit={proc.returncode})")
        return proc.stdout.strip()

    def verify_dev_exit_gate(self):
        """研發階段 Push 前的唯一確定性出口驗收（不綁過程，只驗最終結果）"""
        if self.stage != "dev":
            return

        ws = self.workspace_dir
        if self.verify_cmd_override:
            cmd = self.verify_cmd_override
        elif (ws / "mvnw").exists():
            cmd = "./mvnw -B test"
        elif (ws / "pom.xml").exists():
            cmd = "mvn -B test"
        elif (ws / "gradlew").exists():
            cmd = "./gradlew test --console=plain"
        else:
            log.info("未偵測到 Maven/Gradle 建置檔，略過預設出口測試指令。")
            return

        log.info(f"🔒 執行研發出口驗收指令: {cmd}")
        proc = subprocess.run(cmd, cwd=str(ws), shell=True, capture_output=True, text=True)
        if proc.returncode != 0:
            log.error(f"出口驗收未通過，阻斷推送:\n{proc.stdout[-2000:]}\n{proc.stderr[-1000:]}")
            raise RuntimeError("研發階段出口測試未通過，已中止 Git Push 以保護分支品質。")

    def detect_and_push_changes(self) -> tuple[List[str], Optional[str]]:
        """自動偵測變更檔案並執行 Git Commit & Push"""
        repo_cwd = str(self.workspace_dir)
        chk = subprocess.run(["git", "-C", repo_cwd, "rev-parse", "--is-inside-work-tree"], capture_output=True)
        if chk.returncode != 0:
            return [], None

        status = subprocess.run(
            ["git", "-C", repo_cwd, "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        )
        changed = [line[3:].strip() for line in status.stdout.splitlines() if line.strip()]
        if not changed:
            return [], None

        subprocess.run(["git", "-C", repo_cwd, "add", "-A"], check=True)
        msg = f"🤖 [{self.display_name}] 完成 WI #{self.work_item_id} {self.stage} 階段交付"
        subprocess.run(["git", "-C", repo_cwd, "commit", "-m", msg], check=True)
        sha = subprocess.run(
            ["git", "-C", repo_cwd, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        if self.target_repo_url:
            subprocess.run(
                ["git", "-C", repo_cwd, "push", "origin", f"HEAD:{self.target_branch}"],
                check=True,
            )
        return changed, sha

    def get_work_item(self) -> Dict[str, Any]:
        resp = self.session.get(
            f"{self.base_url}/wit/workitems/{self.work_item_id}",
            params={"$expand": "all", "api-version": "7.1"},
        )
        resp.raise_for_status()
        return resp.json()

    def get_comments(self) -> List[Dict[str, Any]]:
        resp = self.session.get(
            f"{self.base_url}/wit/workItems/{self.work_item_id}/comments",
            params={"api-version": "7.1-preview.4", "$top": 50, "order": "asc"},
        )
        return resp.json().get("comments", []) if resp.status_code == 200 else []

    def post_discussion_and_finish_tag(self, summary: str, files: List[str], commit_hash: Optional[str]):
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        git_md = ""
        if commit_hash:
            short = commit_hash[:8]
            if self.project_web_url:
                git_md = (
                    f"**Git 分支：** [{self.target_branch}]({self.project_web_url}/-/tree/{self.target_branch}) | "
                    f"**Commit：** [{short}]({self.project_web_url}/-/commit/{commit_hash})\n\n"
                )
            else:
                git_md = f"**Git 分支：** `{self.target_branch}` | **Commit：** `{short}`\n\n"

        files_md = ""
        if files:
            items = "\n".join(f"- `{f}`" for f in files)
            files_md = f"### 產出與異動檔案\n{items}\n\n"

        text = (
            f"## 🤖 {self.display_name} 報告 ({now_utc})\n\n"
            f"{summary}\n\n"
            f"{git_md}{files_md}---\n"
            f"*確認無誤請將卡片拖至下一欄；若需補充意見原地重跑，請留言後移除 `{self.tag_prefix}:done` 標籤。*"
        )
        self.session.post(
            f"{self.base_url}/wit/workItems/{self.work_item_id}/comments",
            params={"api-version": "7.1-preview.4"},
            json={"text": text},
        ).raise_for_status()

        self._update_tags(remove_running=True, add_done=True)

    def _update_tags(self, remove_running: bool = True, add_done: bool = False):
        url = f"{self.base_url}/wit/workitems/{self.work_item_id}"
        resp = self.session.get(url, params={"$fields": "System.Tags", "api-version": "7.1"})
        resp.raise_for_status()
        raw = resp.json().get("fields", {}).get("System.Tags", "")
        tags = [t.strip() for t in raw.split(";") if t.strip()] if raw else []

        running_tag = f"{self.tag_prefix}:running"
        done_tag = f"{self.tag_prefix}:done"

        if remove_running:
            tags = [t for t in tags if t != running_tag]
        if add_done and done_tag not in tags:
            tags.append(done_tag)

        self.session.patch(
            url,
            params={"api-version": "7.1"},
            json=[{"op": "add", "path": "/fields/System.Tags", "value": "; ".join(tags)}],
            headers={"Content-Type": "application/json-patch+json"},
        ).raise_for_status()

    def run(self):
        log.info(f"=== 啟動 {self.display_name} (WI #{self.work_item_id}) ===")
        try:
            self.prepare_repo()
            wi = self.get_work_item()
            comments = self.get_comments()
            ctx_file = self.build_context_file(wi, comments)

            summary = self.run_opencode(ctx_file)
            self.verify_dev_exit_gate()

            changed_files, commit_sha = self.detect_and_push_changes()
            self.post_discussion_and_finish_tag(summary, changed_files, commit_sha)
            log.info(f"=== {self.display_name} 完成 ===")
        except Exception as e:
            log.exception(f"執行失敗，自動清理 running 鎖定標籤: {e}")
            try:
                self._update_tags(remove_running=True, add_done=False)
            except Exception:
                pass
            sys.exit(1)


if __name__ == "__main__":
    AdoOpenCodeRunner().run()
