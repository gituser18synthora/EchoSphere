import { useEffect, useMemo, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { Icon, type IconName } from "@/components/Icon";
import { StatusChip, ToastRegion } from "@/components/ui";
import { useApp } from "@/state/AppContext";
import { useAsync } from "@/hooks/useAsync";
import { listAlerts, listBots, listTenants } from "@/services/api";
import type { Role } from "@/types/domain";
import aurexionLogo from "@/assets/brand/Aurexion-logo.svg";

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

export function navFor(role: Role, criticalAlerts: number): NavSection[] {
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
          { to: "/admin/platform-config", label: "Platform Configuration", icon: "settings" },
          { to: "/admin/regional-settings", label: "Regional & Currency Settings", icon: "globe" },
          { to: "/admin/governance", label: "AI Governance", icon: "brain" },
          { to: "/admin/voice", label: "Voice Platform", icon: "phone" },
          { to: "/admin/knowledge", label: "Knowledge", icon: "book" },
          { to: "/admin/knowledge-chunks", label: "Chunk Review", icon: "database" },
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
        { to: "/t/voices", label: "Cloned Voices", icon: "mic" },
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
  admin: "Super Admin", t: "Workspace", tenants: "Organizations",
  "platform-config": "Platform Configuration", profile: "My Profile",
  "regional-settings": "Regional & Currency Settings", countries: "Countries",
  "data-regions": "Data Regions", currencies: "Currencies", "exchange-rates": "Exchange Rates",
  onboarding: "Tenant Onboarding", subscriptions: "Subscriptions", billing: "Billing",
  usage: "Usage", governance: "AI Governance", voice: "Voice Platform",
  knowledge: "Knowledge", "knowledge-chunks": "Chunk Review",
  workflows: "Workflows", monitoring: "Monitoring",
  security: "Security", reports: "Reports", bots: "My VoiceBots", voices: "Cloned Voices",
  channels: "Channels", analytics: "Analytics", conversations: "Conversation Review",
  team: "Team", integrations: "Integrations", settings: "Settings",
  overview: "Overview", prompts: "Prompts", intents: "Intents & Entities",
  apis: "APIs", testing: "Testing", publish: "Publish",
};

const roleLabel = (role: Role) =>
  role === "super_admin" ? "Super Admin" : role === "tenant_admin" ? "Tenant Admin" : "Tenant User";

export default function AppShell() {
  const { user, signOut, theme, toggleTheme } = useApp();
  const location = useLocation();
  const navigate = useNavigate();
  const [alertsOpen, setAlertsOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  const isSuper = user?.role === "super_admin";
  const alertsQ = useAsync(listAlerts, []);
  const botsQ = useAsync(listBots, []);
  const tenantsQ = useAsync(() => (isSuper ? listTenants() : Promise.resolve([])), [isSuper]);

  const critical = (alertsQ.data ?? []).filter((a) => a.status === "open" && (a.severity === "critical" || a.severity === "serious")).length;
  const sections = useMemo(() => navFor(user!.role, critical), [user, critical]);

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

  useEffect(() => {
    setMobileNav(false);
  }, [location.pathname]);

  const crumbs = useMemo(() => {
    const parts = location.pathname.split("/").filter(Boolean);
    const acc: { to: string; label: string }[] = [];
    let path = "";
    for (const p of parts) {
      path += `/${p}`;
      const bot = botsQ.data?.find((b) => b.id === p);
      const tenant = tenantsQ.data?.find((t) => t.id === p);
      const label =
        p === "t"
          ? user?.tenantName ?? "Workspace"
          : bot?.name ?? tenant?.name ?? crumbNames[p] ?? p;
      acc.push({ to: path, label });
    }
    return acc;
  }, [location.pathname, botsQ.data, tenantsQ.data, user?.tenantName]);

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

  const homeTo = isSuper ? "/admin" : "/t";
  const profileTo = isSuper ? "/admin/profile" : "/t/profile";

  return (
    <div className="shell">
      <header className="topbar" ref={popRef}>
        <Link to={homeTo} className="topbar-brand">
          <img src={aurexionLogo} alt="Aurexion" className="topbar-logo" />
          <span className="topbar-brand-sep" aria-hidden />
          <div className="topbar-product">
            <span className="topbar-product-name">EchoSphere</span>
            <span className="topbar-product-sub">VoiceBot Platform</span>
          </div>
        </Link>
        <div className="topbar-search-container">
        <div className="topbar-search">
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
          <span className="kbd">ctrl + K</span>
          {searchOpen && searchResults.length > 0 && (
            <div className="menu" style={{ top: "calc(100% + 6px)", left: 0, right: 0 }}>
              {searchResults.map((r) => (
                <button key={r.to} type="button" className="menu-item" onMouseDown={() => { navigate(r.to); setSearch(""); }}>
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

        <div className="topbar-actions">
          <button
            type="button"
            className="topbar-icon-btn topbar-menu-toggle"
            aria-label="Open navigation"
            onClick={() => setMobileNav((o) => !o)}
          >
            <Icon name="menu" size={20} />
          </button>

          <nav className="topbar-nav" aria-label="Quick links">
            <Link to={homeTo}>Home</Link>
            <span className="topbar-divider" aria-hidden />
            <button type="button" className="topbar-link" onClick={toggleTheme}>
              {theme === "light" ? "Dark" : "Light"}
            </button>
          </nav>

          <div style={{ position: "relative" }}>
            <button
              type="button"
              className="topbar-icon-btn"
              aria-label={`Alerts (${critical} critical open)`}
              onClick={() => { setAlertsOpen((o) => !o); setProfileOpen(false); }}
            >
              <Icon name="bell" size={22} />
              {critical > 0 && <span className="topbar-badge">{critical}</span>}
            </button>
            {alertsOpen && (
              <div className="topbar-alerts">
                <div className="topbar-alerts-head">
                  <h3>Notifications</h3>
                  <button type="button" aria-label="Close alerts" onClick={() => setAlertsOpen(false)}>
                    <Icon name="x" size={16} />
                  </button>
                </div>
                <div style={{ maxHeight: 360, overflowY: "auto", padding: 10 }}>
                  {(alertsQ.data ?? []).slice(0, 6).map((a) => (
                    <button
                      key={a.id}
                      type="button"
                      className="menu-item"
                      style={{
                        width: "100%",
                        alignItems: "flex-start",
                        border: "1px solid var(--hairline)",
                        borderRadius: 8,
                        marginBottom: 8,
                        background: "var(--surface)",
                        padding: 10,
                      }}
                      onClick={() => {
                        setAlertsOpen(false);
                        navigate(user!.role === "super_admin" ? "/admin/monitoring" : "/t");
                      }}
                    >
                      <span className={`health-dot ${a.severity}`} style={{ marginTop: 5 }} />
                      <span className="col" style={{ gap: 1, alignItems: "flex-start" }}>
                        <span style={{ fontSize: 12.5, lineHeight: 1.35, fontWeight: 600 }}>{a.title}</span>
                        <span className="t-micro">{a.source}</span>
                      </span>
                    </button>
                  ))}
                  {(alertsQ.data ?? []).length === 0 && (
                    <div className="col" style={{ alignItems: "center", padding: "28px 8px", gap: 6 }}>
                      <Icon name="bell" size={28} />
                      <span className="t-strong" style={{ fontSize: 13, color: "var(--ink-3)" }}>No notifications</span>
                      <span className="t-micro">You're all caught up!</span>
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>

          <div style={{ position: "relative" }}>
            <button
              type="button"
              className="topbar-user"
              aria-label="Profile menu"
              onClick={() => { setProfileOpen((o) => !o); setAlertsOpen(false); }}
            >
              <div className="topbar-user-avatar">
                <Icon name="user" size={22} />
                <span className="presence" aria-hidden />
              </div>
              <div className="topbar-user-meta">
                <span className="topbar-user-name">{user!.name}</span>
                <span className="topbar-user-role">{roleLabel(user!.role)}</span>
              </div>
            </button>
            {profileOpen && (
              <div className="topbar-menu">
                <div style={{ padding: "10px 16px 8px" }}>
                  <div className="t-strong" style={{ fontSize: 13 }}>{user!.name}</div>
                  <div className="t-micro">{user!.email}</div>
                  <div className="mt-8">
                    <StatusChip status="active" label={roleLabel(user!.role)} />
                  </div>
                  {user!.tenantName && <div className="t-micro mt-8">{user!.tenantName}</div>}
                </div>
                <div className="menu-sep" />
                <button type="button" className="menu-item" onClick={() => { setProfileOpen(false); navigate(profileTo); }}>
                  <Icon name="user" size={14} /> My profile
                </button>
                <button
                  type="button"
                  className="menu-item"
                  onClick={() => { setProfileOpen(false); navigate(profileTo, { state: { tab: "security" } }); }}
                >
                  <Icon name="shield" size={14} /> Change password
                </button>
                <button type="button" className="menu-item" onClick={toggleTheme}>
                  <Icon name={theme === "light" ? "moon" : "sun"} size={14} />
                  {theme === "light" ? "Dark mode" : "Light mode"}
                </button>
                <div className="menu-sep" />
                <button type="button" className="menu-item danger" onClick={() => { signOut(); navigate("/login"); }}>
                  <Icon name="logout" size={14} /> Log out
                </button>
              </div>
            )}
          </div>
        </div>
        </div>
      </header>

      <div className="breadcrumb-strip">
        <nav className="breadcrumbs" aria-label="Breadcrumb">
          {crumbs.map((c, i) => (
            <span key={c.to} className="row gap-6">
              {i > 0 && <Icon name="chevron-right" size={14} />}
              {i === crumbs.length - 1 ? (
                <span className="crumb-current truncate" style={{ maxWidth: 320 }}>{c.label}</span>
              ) : (
                <Link to={c.to}>{c.label}</Link>
              )}
            </span>
          ))}
        </nav>
      </div>

      <div className="shell-body">
        {mobileNav && (
          <button
            type="button"
            className="sidebar-backdrop"
            aria-label="Close navigation"
            onClick={() => setMobileNav(false)}
          />
        )}
        <aside
          className={`sidebar${collapsed ? " collapsed" : ""}${mobileNav ? " mobile-open" : ""}`}
          aria-label="Primary navigation"
        >
          <button
            type="button"
            className="sidebar-collapse"
            aria-label={mobileNav ? "Close navigation" : collapsed ? "Expand sidebar" : "Collapse sidebar"}
            onClick={() => {
              if (mobileNav) {
                setMobileNav(false);
                return;
              }
              setCollapsed((c) => !c);
            }}
          >
            <Icon name={mobileNav || !collapsed ? "chevron-left" : "chevron-right"} size={14} />
          </button>

          <div className="sidebar-role">
            <Icon name={user!.role === "super_admin" ? "shield" : "building"} size={13} />
            <span className="truncate">{user!.role === "super_admin" ? "Platform Control" : user!.tenantName}</span>
          </div>

          <nav className="sidebar-nav">
            {sections.map((s, i) => (
              <div key={i} className="col" style={{ gap: 0 }}>
                {s.title && <div className="sidebar-section">{s.title}</div>}
                {s.items.map((it) => (
                  <NavLink
                    key={it.to}
                    to={it.to}
                    end={it.to === "/admin" || it.to === "/t"}
                    title={collapsed ? it.label : undefined}
                    className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
                    style={{ position: "relative" }}
                  >
                    <Icon name={it.icon} size={16} />
                    <span className="nav-label">{it.label}</span>
                    {it.badge ? <span className="nav-badge">{it.badge}</span> : null}
                  </NavLink>
                ))}
              </div>
            ))}
          </nav>

          <div className="sidebar-foot">
            <button type="button" className="nav-item" title={collapsed ? "Sign out" : undefined} onClick={() => { signOut(); navigate("/login"); }}>
              <Icon name="logout" size={16} />
              <span className="nav-label">Sign out</span>
            </button>
          </div>
        </aside>

        <div className="main">
          <main className="page">
            <div className="page-narrow">
              <Outlet />
            </div>
          </main>
        </div>
      </div>
      <ToastRegion />
    </div>
  );
}
