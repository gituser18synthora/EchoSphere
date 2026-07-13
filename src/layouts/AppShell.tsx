import { useEffect, useMemo, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { Icon, type IconName } from "@/components/Icon";
import { Avatar, StatusChip, ToastRegion } from "@/components/ui";
import { useApp } from "@/state/AppContext";
import { useAsync } from "@/hooks/useAsync";
import { listAlerts, listBots, listTenants } from "@/services/api";
import type { Role } from "@/types/domain";

interface NavEntry {
  to: string;
  label: string;
  icon: IconName;
  badge?: number;
}
interface NavSection {
  title?: string;
  items: NavEntry[];
}

function navFor(role: Role, criticalAlerts: number): NavSection[] {
  if (role === "super_admin") {
    return [
      { items: [{ to: "/admin", label: "Dashboard", icon: "dashboard" }] },
      {
        title: "Tenants",
        items: [
          { to: "/admin/tenants", label: "Organizations", icon: "building" },
          { to: "/admin/onboarding", label: "Tenant Onboarding", icon: "rocket" },
          { to: "/admin/subscriptions", label: "Subscriptions", icon: "layers" },
          { to: "/admin/billing", label: "Billing", icon: "card" },
          { to: "/admin/usage", label: "Usage", icon: "chart" },
        ],
      },
      {
        title: "Platform",
        items: [
          { to: "/admin/governance", label: "AI Governance", icon: "brain" },
          { to: "/admin/voice", label: "Voice Platform", icon: "phone" },
          { to: "/admin/knowledge", label: "Knowledge", icon: "book" },
          { to: "/admin/workflows", label: "Workflows", icon: "workflow" },
        ],
      },
      {
        title: "Operations",
        items: [
          { to: "/admin/monitoring", label: "Monitoring", icon: "activity", badge: criticalAlerts },
          { to: "/admin/security", label: "Security", icon: "shield" },
          { to: "/admin/reports", label: "Reports", icon: "trend" },
        ],
      },
    ];
  }
  return [
    { items: [{ to: "/t", label: "Dashboard", icon: "dashboard" }] },
    {
      title: "Build",
      items: [
        { to: "/t/bots", label: "My VoiceBots", icon: "bot" },
        { to: "/t/knowledge", label: "Knowledge Hub", icon: "book" },
        { to: "/t/workflows", label: "Workflows", icon: "workflow" },
        { to: "/t/channels", label: "Channels", icon: "plug" },
      ],
    },
    {
      title: "Operate",
      items: [
        { to: "/t/analytics", label: "Analytics", icon: "trend" },
        { to: "/t/conversations", label: "Conversation Review", icon: "headphones" },
      ],
    },
    {
      title: "Manage",
      items: [
        { to: "/t/team", label: "Team", icon: "users" },
        { to: "/t/integrations", label: "Integrations", icon: "zap" },
        { to: "/t/settings", label: "Settings", icon: "settings" },
      ],
    },
  ];
}

const crumbNames: Record<string, string> = {
  admin: "Super Admin", t: "Meridian Health Group", tenants: "Organizations",
  onboarding: "Tenant Onboarding", subscriptions: "Subscriptions", billing: "Billing",
  usage: "Usage", governance: "AI Governance", voice: "Voice Platform",
  knowledge: "Knowledge", workflows: "Workflows", monitoring: "Monitoring",
  security: "Security", reports: "Reports", bots: "My VoiceBots",
  channels: "Channels", analytics: "Analytics", conversations: "Conversation Review",
  team: "Team", integrations: "Integrations", settings: "Settings",
  overview: "Overview", prompts: "Prompts", intents: "Intents & Entities",
  apis: "APIs", testing: "Testing", publish: "Publish",
};

export default function AppShell() {
  const { user, signOut, theme, toggleTheme, toast } = useApp();
  const location = useLocation();
  const navigate = useNavigate();
  const [alertsOpen, setAlertsOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  const alertsQ = useAsync(listAlerts, []);
  const botsQ = useAsync(listBots, []);
  const tenantsQ = useAsync(listTenants, []);

  const critical = (alertsQ.data ?? []).filter((a) => a.status === "open" && (a.severity === "critical" || a.severity === "serious")).length;
  const sections = useMemo(() => navFor(user!.role, critical), [user, critical]);

  /* ⌘K / Ctrl-K focuses global search */
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  /* close popovers on outside click */
  useEffect(() => {
    const h = (e: MouseEvent) => {
      if (popRef.current && !popRef.current.contains(e.target as Node)) {
        setAlertsOpen(false);
        setProfileOpen(false);
      }
    };
    window.addEventListener("mousedown", h);
    return () => window.removeEventListener("mousedown", h);
  }, []);

  const crumbs = useMemo(() => {
    const parts = location.pathname.split("/").filter(Boolean);
    const acc: { to: string; label: string }[] = [];
    let path = "";
    for (const p of parts) {
      path += `/${p}`;
      const bot = botsQ.data?.find((b) => b.id === p);
      const tenant = tenantsQ.data?.find((t) => t.id === p);
      acc.push({ to: path, label: bot?.name ?? tenant?.name ?? crumbNames[p] ?? p });
    }
    return acc;
  }, [location.pathname, botsQ.data, tenantsQ.data]);

  const searchResults = useMemo(() => {
    if (search.trim().length < 2) return [];
    const q = search.toLowerCase();
    const results: { label: string; sub: string; to: string; icon: IconName }[] = [];
    if (user!.role === "tenant_admin") {
      botsQ.data?.forEach((b) => {
        if (b.name.toLowerCase().includes(q) || b.useCase.toLowerCase().includes(q))
          results.push({ label: b.name, sub: `VoiceBot · ${b.status.replace("_", " ")}`, to: `/t/bots/${b.id}/overview`, icon: "bot" });
      });
    } else {
      tenantsQ.data?.forEach((t) => {
        if (t.name.toLowerCase().includes(q) || t.domain.toLowerCase().includes(q))
          results.push({ label: t.name, sub: `Tenant · ${t.plan}`, to: `/admin/tenants/${t.id}`, icon: "building" });
      });
    }
    return results.slice(0, 6);
  }, [search, user, botsQ.data, tenantsQ.data]);

  return (
    <div className="shell">
      <aside className="sidebar" aria-label="Primary navigation">
        <div className="sidebar-brand">
          <div className="sidebar-brand-logo"><Icon name="mic" size={17} /></div>
          <div className="sidebar-brand-name">
            EchoSphere
            <small>Aurexion</small>
          </div>
        </div>
        <div className="sidebar-role">
          <Icon name={user!.role === "super_admin" ? "shield" : "building"} size={13} />
          <span className="truncate">{user!.role === "super_admin" ? "Platform Control" : user!.tenantName}</span>
        </div>
        <nav className="sidebar-nav">
          {sections.map((s, i) => (
            <div key={i} className="col" style={{ gap: 1 }}>
              {s.title && <div className="sidebar-section">{s.title}</div>}
              {s.items.map((it) => (
                <NavLink
                  key={it.to}
                  to={it.to}
                  end={it.to === "/admin" || it.to === "/t"}
                  className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
                >
                  <Icon name={it.icon} size={16} />
                  {it.label}
                  {it.badge ? <span className="nav-badge">{it.badge}</span> : null}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <button className="nav-item" onClick={toggleTheme}>
            <Icon name={theme === "light" ? "moon" : "sun"} size={16} />
            {theme === "light" ? "Dark mode" : "Light mode"}
          </button>
          <button className="nav-item" onClick={() => { signOut(); navigate("/login"); }}>
            <Icon name="logout" size={16} />
            Sign out
          </button>
        </div>
      </aside>

      <div className="main">
        <header className="header" ref={popRef}>
          <nav className="breadcrumbs" aria-label="Breadcrumb">
            {crumbs.map((c, i) => (
              <span key={c.to} className="row gap-6">
                {i > 0 && <Icon name="chevron-right" size={12} />}
                {i === crumbs.length - 1 ? (
                  <span className="crumb-current truncate" style={{ maxWidth: 260 }}>{c.label}</span>
                ) : (
                  <Link to={c.to}>{c.label}</Link>
                )}
              </span>
            ))}
          </nav>

          <div className="header-actions">
            <div className="header-search">
              <Icon name="search" size={14} />
              <input
                ref={searchRef}
                className="input"
                placeholder={user!.role === "super_admin" ? "Search tenants…" : "Search bots…"}
                value={search}
                aria-label="Global search"
                onChange={(e) => { setSearch(e.target.value); setSearchOpen(true); }}
                onFocus={() => setSearchOpen(true)}
                onBlur={() => setTimeout(() => setSearchOpen(false), 150)}
              />
              <span className="kbd">⌘K</span>
              {searchOpen && searchResults.length > 0 && (
                <div className="menu" style={{ top: "calc(100% + 6px)", left: 0, right: 0 }}>
                  {searchResults.map((r) => (
                    <button key={r.to} className="menu-item" onMouseDown={() => { navigate(r.to); setSearch(""); }}>
                      <Icon name={r.icon} size={14} />
                      <span className="col" style={{ gap: 0, alignItems: "flex-start" }}>
                        <span>{r.label}</span>
                        <span className="t-micro">{r.sub}</span>
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </div>

            <div style={{ position: "relative" }}>
              <button
                className="btn-icon"
                aria-label={`Alerts (${critical} critical open)`}
                onClick={() => { setAlertsOpen((o) => !o); setProfileOpen(false); }}
              >
                <Icon name="bell" />
                {critical > 0 && (
                  <span style={{ position: "absolute", top: 4, right: 4, width: 8, height: 8, borderRadius: "50%", background: "var(--status-critical)", border: "2px solid var(--surface)" }} />
                )}
              </button>
              {alertsOpen && (
                <div className="menu" style={{ right: 0, top: "calc(100% + 6px)", width: 340 }}>
                  <div className="row-between" style={{ padding: "6px 10px 8px" }}>
                    <span className="t-strong" style={{ fontSize: 13 }}>Alerts</span>
                    <StatusChip status={critical > 0 ? "critical" : "good"} label={critical > 0 ? `${critical} need attention` : "All clear"} />
                  </div>
                  <div className="menu-sep" />
                  {(alertsQ.data ?? []).slice(0, 4).map((a) => (
                    <button key={a.id} className="menu-item" style={{ alignItems: "flex-start" }} onClick={() => {
                      setAlertsOpen(false);
                      navigate(user!.role === "super_admin" ? "/admin/monitoring" : "/t");
                    }}>
                      <span className={`health-dot ${a.severity}`} style={{ marginTop: 5 }} />
                      <span className="col" style={{ gap: 1, alignItems: "flex-start" }}>
                        <span style={{ fontSize: 12.5, lineHeight: 1.35 }}>{a.title}</span>
                        <span className="t-micro">{a.source}</span>
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </div>

            <div style={{ position: "relative" }}>
              <button className="row gap-6" style={{ padding: 4, borderRadius: 8 }} aria-label="Profile menu" onClick={() => { setProfileOpen((o) => !o); setAlertsOpen(false); }}>
                <Avatar name={user!.name} />
                <Icon name="chevron-down" size={13} />
              </button>
              {profileOpen && (
                <div className="menu" style={{ right: 0, top: "calc(100% + 6px)", width: 230 }}>
                  <div style={{ padding: "8px 10px" }}>
                    <div className="t-strong" style={{ fontSize: 13 }}>{user!.name}</div>
                    <div className="t-micro">{user!.email}</div>
                    <div className="mt-8"><StatusChip status="active" label={user!.role === "super_admin" ? "Super Admin" : "Tenant Admin"} /></div>
                  </div>
                  <div className="menu-sep" />
                  <button className="menu-item" onClick={() => { setProfileOpen(false); navigate("/login"); }}>
                    <Icon name="refresh" size={14} /> Switch role
                  </button>
                  <button className="menu-item" onClick={() => { setProfileOpen(false); toast("Profile settings coming with SSO integration", "info"); }}>
                    <Icon name="user" size={14} /> Profile settings
                  </button>
                  <div className="menu-sep" />
                  <button className="menu-item danger" onClick={() => { signOut(); navigate("/login"); }}>
                    <Icon name="logout" size={14} /> Sign out
                  </button>
                </div>
              )}
            </div>
          </div>
        </header>

        <main className="page">
          <div className="page-narrow">
            <Outlet />
          </div>
        </main>
      </div>
      <ToastRegion />
    </div>
  );
}
