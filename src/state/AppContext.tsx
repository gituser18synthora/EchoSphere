import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from "react";
import type { Role } from "@/types/domain";

export interface SessionUser {
  name: string;
  email: string;
  role: Role;
  tenantName?: string;
}

interface Toast {
  id: number;
  message: string;
  kind: "good" | "error" | "info";
}

interface AppState {
  user: SessionUser | null;
  signIn: (user: SessionUser) => void;
  signOut: () => void;
  theme: "light" | "dark";
  toggleTheme: () => void;
  toasts: Toast[];
  toast: (message: string, kind?: Toast["kind"]) => void;
}

const Ctx = createContext<AppState | null>(null);

const USER_KEY = "echosphere.session";
const THEME_KEY = "echosphere.theme";

export function AppProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<SessionUser | null>(() => {
    try {
      const raw = localStorage.getItem(USER_KEY);
      return raw ? (JSON.parse(raw) as SessionUser) : null;
    } catch {
      return null;
    }
  });
  const [theme, setTheme] = useState<"light" | "dark">(
    () => (localStorage.getItem(THEME_KEY) as "light" | "dark") || "light",
  );
  const [toasts, setToasts] = useState<Toast[]>([]);
  const toastId = useRef(0);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  const signIn = useCallback((u: SessionUser) => {
    setUser(u);
    localStorage.setItem(USER_KEY, JSON.stringify(u));
  }, []);

  const signOut = useCallback(() => {
    setUser(null);
    localStorage.removeItem(USER_KEY);
  }, []);

  const toggleTheme = useCallback(() => setTheme((t) => (t === "light" ? "dark" : "light")), []);

  const toast = useCallback((message: string, kind: Toast["kind"] = "good") => {
    const id = ++toastId.current;
    setToasts((ts) => [...ts, { id, message, kind }]);
    setTimeout(() => setToasts((ts) => ts.filter((t) => t.id !== id)), 3800);
  }, []);

  const value = useMemo(
    () => ({ user, signIn, signOut, theme, toggleTheme, toasts, toast }),
    [user, signIn, signOut, theme, toggleTheme, toasts, toast],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): AppState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useApp must be used inside AppProvider");
  return ctx;
}
