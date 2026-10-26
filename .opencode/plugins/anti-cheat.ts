/**
 * OpenCode Harness 底線防作弊護欄 (Anti-Cheat Guardrail)
 * 不限制 Agent 的探索與實作流程，僅在工具層攔截「跳過測試」與「隱藏失敗測試」行為。
 */
export default async () => {
  return {
    "tool.execute.before": async (input: any, output: any) => {
      if (input.tool === "bash") {
        const cmd = String(output.args?.command || "");
        const skipPattern =
          /(-DskipTests|-Dmaven\.test\.skip\s*=\s*true|-x\s+test|-x\s+check|-Dcheckstyle\.skip\s*=\s*true)/i;
        if (skipPattern.test(cmd)) {
          throw new Error(
            "Harness 護欄攔截：禁止使用跳過測試或檢查的參數 (-DskipTests / -x test / skip=true)，請正面修復問題。"
          );
        }
      }

      if (input.tool === "write" || input.tool === "edit") {
        const filePath = String(output.args?.filePath || output.args?.path || "");
        const content = String(output.args?.content || output.args?.newString || "");
        if (/test/i.test(filePath) && /@(Disabled|Ignore)\b/.test(content)) {
          throw new Error(
            "Harness 護欄攔截：禁止在測試檔案中使用 @Disabled 或 @Ignore 隱藏失敗測試，請修正業務邏輯或測試設定。"
          );
        }
      }
    },
  };
};
