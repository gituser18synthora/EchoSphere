/* Regional & Currency Settings — Super Admin management of geographic and
   monetary master data: Countries, Data Regions, Currencies and Exchange
   Rates. Moved out of Platform Configuration so that page stays focused on
   product/AI configuration; the underlying APIs, permissions and the shared
   MasterPanel implementation are unchanged.

   The active tab lives in the URL (/admin/regional-settings/:tab) following
   the Studio tab convention, which gives deep links and per-tab breadcrumbs. */

import { useNavigate, useParams } from "react-router-dom";
import { MasterDataPage, REGIONAL_SPECS } from "@/pages/admin/PlatformConfig";
import type { MasterType } from "@/services/api";

const VALID_TABS = new Set<string>(REGIONAL_SPECS.map((s) => s.mtype));

export default function RegionalSettings() {
  const { tab } = useParams();
  const navigate = useNavigate();
  const active = (tab && VALID_TABS.has(tab) ? tab : REGIONAL_SPECS[0].mtype) as MasterType;
  return (
    <MasterDataPage
      title="Regional & Currency Settings"
      sub="Manage geographic regions, supported currencies, and platform exchange rates."
      specs={REGIONAL_SPECS}
      tab={active}
      onTabChange={(next) => navigate(`/admin/regional-settings/${next}`)}
    />
  );
}
