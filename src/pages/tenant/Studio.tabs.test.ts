/* Studio tab registry: tabs carrying permission codes are shown only when the
   session holds one of them. Overview, Knowledge, Prompts, Voice, Workflows
   and Testing are open to every tenant role — the Tenant User working set.
   Turn Detection is Tenant Admin-only; the remaining restricted tabs use
   management permissions. (The API independently enforces every rule.) */

import { describe, expect, it } from "vitest";
import { visibleStudioTabs } from "./Studio";

const TENANT_USER_PERMS = [
  "bots.view", "knowledge.view", "conversations.view", "analytics.view",
  "view_tenant_profile", "change_own_password",
  "manage_knowledge", "upload_knowledge_documents", "retry_knowledge_ingestion",
  "manage_prompts", "manage_voices", "manage_workflows", "manage_testing",
];

describe("Studio tab permission gating", () => {
  it("shows every tab when the session holds all permissions (admin)", () => {
    const ids = visibleStudioTabs(() => true, "tenant_admin").map((t) => t.id);
    expect(ids).toEqual([
      "overview", "knowledge", "prompts", "voice", "turn-detection", "intents", "apis",
      "workflows", "channels", "testing", "analytics", "publish",
    ]);
  });

  it("shows exactly the six allowed sections to a tenant user", () => {
    const ids = visibleStudioTabs((c) => TENANT_USER_PERMS.includes(c), "tenant_user").map((t) => t.id);
    expect(ids).toEqual([
      "overview", "knowledge", "prompts", "voice", "workflows", "testing",
    ]);
  });
});
