import { Navigate, Route, Routes } from "react-router-dom";
import { useApp } from "@/state/AppContext";
import AppShell from "@/layouts/AppShell";
import Login from "@/pages/Login";
import type { Role } from "@/types/domain";

/* Super Admin */
import AdminDashboard from "@/pages/admin/Dashboard";
import Organizations from "@/pages/admin/Organizations";
import TenantDetail from "@/pages/admin/TenantDetail";
import Onboarding from "@/pages/admin/Onboarding";
import Subscriptions from "@/pages/admin/Subscriptions";
import Billing from "@/pages/admin/Billing";
import UsageReport from "@/pages/admin/Usage";
import Governance from "@/pages/admin/Governance";
import VoicePlatform from "@/pages/admin/VoicePlatform";
import KnowledgeAdmin from "@/pages/admin/KnowledgeAdmin";
import KnowledgeChunks from "@/pages/admin/KnowledgeChunks";
import WorkflowsAdmin from "@/pages/admin/WorkflowsAdmin";
import Monitoring from "@/pages/admin/Monitoring";
import Security from "@/pages/admin/Security";
import Reports from "@/pages/admin/Reports";
import PlatformConfig from "@/pages/admin/PlatformConfig";
import RegionalSettings from "@/pages/admin/RegionalSettings";
import Profile from "@/pages/Profile";

/* Tenant Admin */
import TenantDashboard from "@/pages/tenant/Dashboard";
import Bots from "@/pages/tenant/Bots";
import Studio from "@/pages/tenant/Studio";
import KnowledgeHub from "@/pages/tenant/KnowledgeHub";
import TenantVoices from "@/pages/tenant/Voices";
import TenantWorkflows from "@/pages/tenant/Workflows";
import Channels from "@/pages/tenant/Channels";
import Analytics from "@/pages/tenant/Analytics";
import Conversations from "@/pages/tenant/Conversations";
import Team from "@/pages/tenant/Team";
import Integrations from "@/pages/tenant/Integrations";
import Settings from "@/pages/tenant/Settings";

function Guard({ roles, children }: { roles: Role[]; children: React.ReactElement }) {
  const { user } = useApp();
  if (!user) return <Navigate to="/login" replace />;
  if (!roles.includes(user.role))
    return <Navigate to={user.role === "super_admin" ? "/admin" : "/t"} replace />;
  return children;
}

/** Permission-gated route (UI affordance — the API enforces the same
 *  permission server-side). Redirects like Guard does for a role mismatch. */
function Require({ perm, children }: { perm: string; children: React.ReactElement }) {
  const { user, hasPermission } = useApp();
  if (!user) return <Navigate to="/login" replace />;
  if (!hasPermission(perm))
    return <Navigate to={user.role === "super_admin" ? "/admin" : "/t"} replace />;
  return children;
}

export default function App() {
  const { user } = useApp();
  return (
    <Routes>
      <Route path="/login" element={<Login />} />

      <Route path="/admin" element={<Guard roles={["super_admin"]}><AppShell /></Guard>}>
        <Route index element={<AdminDashboard />} />
        <Route path="tenants" element={<Organizations />} />
        <Route path="tenants/:tenantId" element={<TenantDetail />} />
        <Route path="onboarding" element={<Onboarding />} />
        <Route path="subscriptions" element={<Subscriptions />} />
        <Route path="billing" element={<Billing />} />
        <Route path="usage" element={<UsageReport />} />
        <Route path="governance" element={<Governance />} />
        <Route path="voice" element={<VoicePlatform />} />
        <Route path="knowledge" element={<KnowledgeAdmin />} />
        <Route path="knowledge-chunks" element={<KnowledgeChunks />} />
        <Route path="workflows" element={<WorkflowsAdmin />} />
        <Route path="monitoring" element={<Monitoring />} />
        <Route path="security" element={<Security />} />
        <Route path="reports" element={<Reports />} />
        <Route path="platform-config" element={<PlatformConfig />} />
        <Route path="regional-settings" element={<RegionalSettings />} />
        <Route path="regional-settings/:tab" element={<RegionalSettings />} />
        <Route path="profile" element={<Profile />} />
      </Route>

      <Route path="/t" element={<Guard roles={["tenant_admin", "tenant_user"]}><AppShell /></Guard>}>
        <Route index element={<TenantDashboard />} />
        <Route path="bots" element={<Bots />} />
        <Route path="bots/:botId" element={<Studio />} />
        <Route path="bots/:botId/:tab" element={<Studio />} />
        <Route path="knowledge" element={<KnowledgeHub />} />
        <Route path="voices" element={<Require perm="manage_voice_clones"><TenantVoices /></Require>} />
        <Route path="workflows" element={<TenantWorkflows />} />
        <Route path="channels" element={<Require perm="manage_channels"><Channels /></Require>} />
        <Route path="analytics" element={<Analytics />} />
        <Route path="conversations" element={<Conversations />} />
        <Route path="team" element={<Require perm="team.manage"><Team /></Require>} />
        <Route path="integrations" element={<Require perm="integrations.manage"><Integrations /></Require>} />
        <Route path="settings" element={<Require perm="settings.manage"><Settings /></Require>} />
        <Route path="profile" element={<Profile />} />
      </Route>

      <Route
        path="*"
        element={<Navigate to={user ? (user.role === "super_admin" ? "/admin" : "/t") : "/login"} replace />}
      />
    </Routes>
  );
}
