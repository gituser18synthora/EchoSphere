import { useState } from "react";
import { Tabs } from "@/components/ui";
import UsersPanel from "./security/UsersPanel";
import RolesPanel from "./security/RolesPanel";
import AuditLogPanel from "./security/AuditLogPanel";
const tabs = [{ id: "users", label: "Users" }, { id: "roles", label: "Roles" }, { id: "audit", label: "Audit Logs" }];
export default function Security() {
  const [tab, setTab] = useState("users");
  return <>
    <div className="page-head"><div className="page-head-titles"><h1 className="page-title">Security</h1>
      <p className="page-sub">Manage accounts, role capabilities and recorded activity</p></div></div>
    <Tabs tabs={tabs} active={tab} onChange={setTab} />
    <div className="mt-16">{tab === "users" ? <UsersPanel /> : tab === "roles" ? <RolesPanel /> : <AuditLogPanel />}</div>
  </>;
}
