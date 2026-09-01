import type { ReactNode, SVGProps } from "react";

/** Stroke-based 24px icon set (self-contained, lucide-style geometry). */
const paths: Record<string, ReactNode> = {
  dashboard: (
    <>
      <rect x="3" y="3" width="7" height="9" rx="1.5" />
      <rect x="14" y="3" width="7" height="5" rx="1.5" />
      <rect x="14" y="12" width="7" height="9" rx="1.5" />
      <rect x="3" y="16" width="7" height="5" rx="1.5" />
    </>
  ),
  building: (
    <>
      <rect x="4" y="3" width="16" height="18" rx="1.5" />
      <path d="M8 7h2M8 11h2M8 15h2M14 7h2M14 11h2M14 15h2M10 21v-3h4v3" />
    </>
  ),
  users: (
    <>
      <circle cx="9" cy="8" r="3.5" />
      <path d="M2.5 20c.7-3.2 3.3-5 6.5-5s5.8 1.8 6.5 5" />
      <path d="M16 5a3.5 3.5 0 0 1 0 6.5M17.5 15.5c2.2.6 3.6 2 4 4.5" />
    </>
  ),
  user: (
    <>
      <circle cx="12" cy="8" r="3.5" />
      <path d="M5 20c.8-3.4 3.6-5.2 7-5.2s6.2 1.8 7 5.2" />
    </>
  ),
  bot: (
    <>
      <rect x="4" y="8" width="16" height="11" rx="2.5" />
      <path d="M12 8V4.5M9 4.5h6" />
      <circle cx="9" cy="13" r="0.8" fill="currentColor" stroke="none" />
      <circle cx="15" cy="13" r="0.8" fill="currentColor" stroke="none" />
      <path d="M9.5 16.2h5" />
    </>
  ),
  mic: (
    <>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3" />
    </>
  ),
  "mic-off": (
    <>
      <path d="M9 9v5a3 3 0 0 0 5.1 2.1M15 11V6a3 3 0 0 0-5.6-1.5" />
      <path d="M5.5 11.5a6.5 6.5 0 0 0 10.4 5.2M18.5 11.5a6.4 6.4 0 0 1-.8 3.1M12 18v3" />
      <path d="M4 4l16 16" />
    </>
  ),
  phone: (
    <path d="M5 4h4l1.5 4.5-2.2 1.6a12.5 12.5 0 0 0 5.6 5.6l1.6-2.2L20 15v4a1.8 1.8 0 0 1-2 1.8C10.4 20.1 3.9 13.6 3.2 6A1.8 1.8 0 0 1 5 4Z" />
  ),
  book: (
    <>
      <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5V5.5Z" />
      <path d="M4 18.5A2.5 2.5 0 0 1 6.5 16H20M20 18v3" />
    </>
  ),
  file: (
    <>
      <path d="M6 3h8l4 4v14H6V3Z" />
      <path d="M14 3v4h4M9 12h6M9 16h6" />
    </>
  ),
  link: (
    <>
      <path d="M10 14a4 4 0 0 0 6 .5l3-3a4 4 0 1 0-5.7-5.6l-1.2 1.2" />
      <path d="M14 10a4 4 0 0 0-6-.5l-3 3a4 4 0 1 0 5.7 5.6l1.2-1.2" />
    </>
  ),
  workflow: (
    <>
      <circle cx="6" cy="6" r="2.6" />
      <circle cx="18" cy="12" r="2.6" />
      <circle cx="6" cy="18" r="2.6" />
      <path d="M8.6 6H13a2.5 2.5 0 0 1 2.5 2.5v1M8.6 18H13a2.5 2.5 0 0 0 2.5-2.5v-1" />
    </>
  ),
  shield: (
    <>
      <path d="M12 3l7.5 2.8v5.4c0 4.6-3 8.1-7.5 9.8-4.5-1.7-7.5-5.2-7.5-9.8V5.8L12 3Z" />
      <path d="M9 11.6l2.2 2.2L15.4 9.5" />
    </>
  ),
  activity: <path d="M3 12h4l2.5-7 5 14 2.5-7h4" />,
  alert: (
    <>
      <path d="M12 3.5 21.5 20h-19L12 3.5Z" />
      <path d="M12 10v4.5" />
      <circle cx="12" cy="17.3" r="0.8" fill="currentColor" stroke="none" />
    </>
  ),
  bell: (
    <>
      <path d="M6 10a6 6 0 0 1 12 0c0 4 1.5 5.6 2 6.3H4c.5-.7 2-2.3 2-6.3Z" />
      <path d="M10 19.5a2.2 2.2 0 0 0 4 0" />
    </>
  ),
  chart: (
    <>
      <path d="M4 4v16h16" />
      <path d="M8.5 16v-5M13 16V8M17.5 16v-3.5" />
    </>
  ),
  trend: (
    <>
      <path d="M3 17l6-6 4 4 8-8" />
      <path d="M15 7h6v6" />
    </>
  ),
  settings: (
    <>
      <circle cx="12" cy="12" r="3.2" />
      <path d="M12 2.8l1.2 2.6 2.8-.6 1 2.7 2.7 1-.6 2.8 2.1 1.7-2.1 1.7.6 2.8-2.7 1-1 2.7-2.8-.6-1.2 2.6-1.2-2.6-2.8.6-1-2.7-2.7-1 .6-2.8L2.8 12l2.1-1.7-.6-2.8 2.7-1 1-2.7 2.8.6L12 2.8Z" />
    </>
  ),
  search: (
    <>
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="M15.5 15.5 21 21" />
    </>
  ),
  "zoom-in": (
    <>
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="M15.5 15.5 21 21M10.5 8v5M8 10.5h5" />
    </>
  ),
  "zoom-out": (
    <>
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="M15.5 15.5 21 21M8 10.5h5" />
    </>
  ),
  maximize: <path d="M9 3H3v6M15 3h6v6M9 21H3v-6M15 21h6v-6" />,
  grip: (
    <>
      <circle cx="9" cy="5.5" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="15" cy="5.5" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="9" cy="12" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="15" cy="12" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="9" cy="18.5" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="15" cy="18.5" r="1.1" fill="currentColor" stroke="none" />
    </>
  ),
  help: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M9.4 9.2a2.7 2.7 0 0 1 5.2 1c0 1.8-2.6 2.2-2.6 3.8" />
      <circle cx="12" cy="17.2" r="0.8" fill="currentColor" stroke="none" />
    </>
  ),
  "chevron-down": <path d="M6 9l6 6 6-6" />,
  "chevron-up": <path d="M6 15l6-6 6 6" />,
  "chevron-right": <path d="M9 6l6 6-6 6" />,
  "chevron-left": <path d="M15 6l-6 6 6 6" />,
  plus: <path d="M12 5v14M5 12h14" />,
  x: <path d="M6 6l12 12M18 6 6 18" />,
  check: <path d="M4.5 12.5 10 18 19.5 7" />,
  "check-circle": (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M8 12.2l2.7 2.7L16.5 9" />
    </>
  ),
  "x-circle": (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M9 9l6 6M15 9l-6 6" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5.2l3.4 2" />
    </>
  ),
  play: <path d="M7 5.2v13.6L19 12 7 5.2Z" />,
  pause: <path d="M8 5v14M16 5v14" />,
  upload: (
    <>
      <path d="M12 15V4M7.5 8.5 12 4l4.5 4.5" />
      <path d="M4 16v3.5h16V16" />
    </>
  ),
  download: (
    <>
      <path d="M12 4v11M7.5 10.5 12 15l4.5-4.5" />
      <path d="M4 16v3.5h16V16" />
    </>
  ),
  refresh: (
    <>
      <path d="M20 12a8 8 0 1 1-2.3-5.6" />
      <path d="M20 3.5V8h-4.5" />
    </>
  ),
  copy: (
    <>
      <rect x="9" y="9" width="12" height="12" rx="2" />
      <path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1" />
    </>
  ),
  edit: (
    <>
      <path d="M4 20h4L20.5 7.5a2.1 2.1 0 0 0-3-3L5 17l-1 4Z" />
      <path d="M14.5 6.5l3 3" />
    </>
  ),
  trash: (
    <>
      <path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13.5h9l1-13.5" />
      <path d="M10 11v6M14 11v6" />
    </>
  ),
  more: (
    <>
      <circle cx="5" cy="12" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="19" cy="12" r="1.1" fill="currentColor" stroke="none" />
    </>
  ),
  external: (
    <>
      <path d="M14 4h6v6M20 4l-9 9" />
      <path d="M19 13.5V20H4V5h6.5" />
    </>
  ),
  filter: <path d="M4 5h16l-6.2 7.2V19l-3.6-2v-4.8L4 5Z" />,
  globe: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3c2.6 2.5 4 5.6 4 9s-1.4 6.5-4 9c-2.6-2.5-4-5.6-4-9s1.4-6.5 4-9Z" />
    </>
  ),
  message: (
    <path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v10a1.5 1.5 0 0 1-1.5 1.5H9l-5 4V5.5Z" />
  ),
  headphones: (
    <>
      <path d="M4 14v-2a8 8 0 0 1 16 0v2" />
      <rect x="3" y="14" width="4.5" height="6" rx="1.5" />
      <rect x="16.5" y="14" width="4.5" height="6" rx="1.5" />
    </>
  ),
  zap: <path d="M13 2 4.5 13.5H11L10 22l8.5-11.5H13L13 2Z" />,
  key: (
    <>
      <circle cx="8" cy="15" r="4.5" />
      <path d="M11.4 11.6 20 3M16 7l3 3" />
    </>
  ),
  lock: (
    <>
      <rect x="5" y="11" width="14" height="9.5" rx="2" />
      <path d="M8 11V7.5a4 4 0 0 1 8 0V11" />
    </>
  ),
  eye: (
    <>
      <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" />
      <circle cx="12" cy="12" r="3" />
    </>
  ),
  "eye-off": (
    <>
      <path d="M10.7 5.8A9.6 9.6 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a17.6 17.6 0 0 1-3 3.85M6.5 6.7C4 8.6 2.5 12 2.5 12s3.5 6.5 9.5 6.5c1.35 0 2.6-.33 3.72-.85" />
      <path d="M9.9 9.9a3 3 0 0 0 4.24 4.24" />
      <path d="M4 4l16 16" />
    </>
  ),
  "arrow-up": <path d="M12 19V5M6 11l6-6 6 6" />,
  "arrow-down": <path d="M12 5v14M6 13l6 6 6-6" />,
  "arrow-right": <path d="M5 12h14M13 6l6 6-6 6" />,
  rocket: (
    <>
      <path d="M12 15c-1.5-1-3-3.8-2.5-7C10.6 4.5 12.5 3 14.8 2.5c.5 2.3 0 5.2-1.3 7.3" />
      <path d="M12 15c1.7-.4 4.7-.4 7-2.5-1.6-1.5-3.5-2-5.5-1.7M12 15c.4-1.7.4-4.7 2.5-7-.2-.1 0 0 0 0" />
      <path d="M9.5 8C7.5 7.7 5.6 8.2 4 9.7c2.3 2.1 5.3 2.1 7 2.5" />
      <path d="M5.5 15.5c-1.3 1-2 4-2 4s3-.7 4-2M9 13l2 2" />
    </>
  ),
  layers: (
    <>
      <path d="M12 3l9 4.5-9 4.5-9-4.5L12 3Z" />
      <path d="M3 12l9 4.5 9-4.5M3 16.5 12 21l9-4.5" />
    </>
  ),
  version: (
    <>
      <circle cx="12" cy="5" r="2.5" />
      <circle cx="12" cy="19" r="2.5" />
      <path d="M12 7.5v9" />
    </>
  ),
  flag: <path d="M5 21V4c4-2.5 7 2 12 0v10c-5 2-8-2.5-12 0" />,
  star: (
    <path d="M12 3l2.7 5.7 6.1.8-4.5 4.3 1.1 6.1L12 17l-5.4 2.9 1.1-6.1L3.2 9.5l6.1-.8L12 3Z" />
  ),
  dollar: (
    <>
      <path d="M12 3v18" />
      <path d="M16.5 7.5c-.6-1.5-2.2-2.3-4.5-2.3-2.5 0-4.2 1.2-4.2 3.1 0 4.3 9.4 2.1 9.4 6.6 0 2-1.9 3.3-4.7 3.3-2.5 0-4.3-1-4.9-2.7" />
    </>
  ),
  card: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="M3 10h18M7 15h4" />
    </>
  ),
  database: (
    <>
      <ellipse cx="12" cy="5.5" rx="8" ry="2.8" />
      <path d="M4 5.5V12c0 1.5 3.6 2.8 8 2.8s8-1.3 8-2.8V5.5" />
      <path d="M4 12v6.5c0 1.5 3.6 2.8 8 2.8s8-1.3 8-2.8V12" />
    </>
  ),
  cpu: (
    <>
      <rect x="6" y="6" width="12" height="12" rx="1.5" />
      <rect x="10" y="10" width="4" height="4" />
      <path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3" />
    </>
  ),
  volume: (
    <>
      <path d="M4 9v6h4l5 4V5L8 9H4Z" />
      <path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12" />
    </>
  ),
  sun: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M18.7 5.3l-1.4 1.4M6.7 17.3l-1.4 1.4" />
    </>
  ),
  moon: <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5Z" />,
  logout: (
    <>
      <path d="M14 4H5v16h9" />
      <path d="M10 12h11M17 8l4 4-4 4" />
    </>
  ),
  menu: <path d="M4 6h16M4 12h16M4 18h16" />,
  sliders: (
    <>
      <path d="M5 4v6M5 14v6M12 4v2M12 10v10M19 4v10M19 18v2" />
      <circle cx="5" cy="12" r="2" />
      <circle cx="12" cy="8" r="2" />
      <circle cx="19" cy="16" r="2" />
    </>
  ),
  target: (
    <>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="5" />
      <circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none" />
    </>
  ),
  package: (
    <>
      <path d="M12 3l8 4v10l-8 4-8-4V7l8-4Z" />
      <path d="M4 7l8 4 8-4M12 11v10" />
    </>
  ),
  sparkles: (
    <>
      <path d="M12 4l1.8 4.7L18.5 10.5l-4.7 1.8L12 17l-1.8-4.7L5.5 10.5l4.7-1.8L12 4Z" />
      <path d="M19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15Z" />
    </>
  ),
  history: (
    <>
      <path d="M4 12a8 8 0 1 1 2.3 5.6" />
      <path d="M4 20.5V16h4.5" />
      <path d="M12 8v4.5l3 1.8" />
    </>
  ),
  send: <path d="M21 3 3 10.5l7 2.5M21 3l-6 18-4.5-8M21 3 10 13" />,
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5.5" />
      <circle cx="12" cy="7.8" r="0.9" fill="currentColor" stroke="none" />
    </>
  ),
  server: (
    <>
      <rect x="3" y="4" width="18" height="7" rx="1.5" />
      <rect x="3" y="13" width="18" height="7" rx="1.5" />
      <circle cx="7" cy="7.5" r="0.9" fill="currentColor" stroke="none" />
      <circle cx="7" cy="16.5" r="0.9" fill="currentColor" stroke="none" />
    </>
  ),
  plug: (
    <>
      <path d="M9 3v5M15 3v5" />
      <path d="M6 8h12v3a6 6 0 0 1-6 6 6 6 0 0 1-6-6V8Z" />
      <path d="M12 17v4" />
    </>
  ),
  brain: (
    <>
      <path d="M9.5 3.5A3 3 0 0 0 6.5 7c-2 .5-3 2-3 4 0 1.4.6 2.6 1.7 3.4-.2.5-.2 1-.2 1.6a4 4 0 0 0 4.5 4c.7 1 2 1.5 3 1.2V4.8a3 3 0 0 0-3-1.3Z" />
      <path d="M14.5 3.5A3 3 0 0 1 17.5 7c2 .5 3 2 3 4 0 1.4-.6 2.6-1.7 3.4.2.5.2 1 .2 1.6a4 4 0 0 1-4.5 4c-.7 1-2 1.5-3 1.2V4.8a3 3 0 0 1 3-1.3Z" />
    </>
  ),
  wand: (
    <>
      <path d="M4 20 16.5 7.5" />
      <path d="M17.5 3l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7.7-1.8ZM20 11l.5 1.2 1.2.5-1.2.5-.5 1.2-.5-1.2-1.2-.5 1.2-.5.5-1.2ZM8 3.5l.5 1.2 1.2.5-1.2.5L8 7l-.5-1.3L6.2 5.2l1.3-.5L8 3.5Z" />
    </>
  ),
  git: (
    <>
      <circle cx="6" cy="6" r="2.5" />
      <circle cx="6" cy="18" r="2.5" />
      <circle cx="18" cy="9" r="2.5" />
      <path d="M6 8.5v7M18 11.5c0 3-2.5 4-5.5 4" />
    </>
  ),
  undo: (
    <>
      <path d="M8 5 3.5 9.5 8 14" />
      <path d="M3.5 9.5H15a5.5 5.5 0 0 1 0 11h-4" />
    </>
  ),
  redo: (
    <>
      <path d="M16 5l4.5 4.5L16 14" />
      <path d="M20.5 9.5H9a5.5 5.5 0 0 0 0 11h4" />
    </>
  ),
  calendar: (
    <>
      <rect x="4" y="5" width="16" height="16" rx="2" />
      <path d="M4 10h16M8 3v4M16 3v4" />
    </>
  ),
  mail: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="M3 7.5l9 6 9-6" />
    </>
  ),
  whatsapp: (
    <>
      <path d="M12 3a9 9 0 0 0-7.8 13.5L3 21l4.6-1.2A9 9 0 1 0 12 3Z" />
      <path d="M9 8.5c-.4 2.5 2.5 6 6.4 6.5l.8-1.7-2-1.2-.9.8c-1-.5-1.8-1.3-2.3-2.3l.8-.9-1.1-2-1.7.8Z" />
    </>
  ),
  smartphone: (
    <>
      <rect x="7" y="3" width="10" height="18" rx="2" />
      <path d="M11 17.8h2" />
    </>
  ),
  monitor: (
    <>
      <rect x="3" y="4" width="18" height="12.5" rx="1.8" />
      <path d="M9 20.5h6M12 16.5v4" />
    </>
  ),
};

export type IconName = keyof typeof paths & string;

export interface IconProps extends SVGProps<SVGSVGElement> {
  name: IconName;
  size?: number;
}

export function Icon({ name, size = 17, ...rest }: IconProps) {
  const content = paths[name] ?? paths.info;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {content}
    </svg>
  );
}
