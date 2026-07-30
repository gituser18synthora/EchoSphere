/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

const envPort = (env: Record<string, string>, key: string, fallback: number): number => {
  const raw = env[key];
  if (!raw) return fallback;
  const port = Number(raw);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`${key} must be an integer between 1 and 65535.`);
  }
  return port;
};

export default defineConfig(({ mode }) => {
  // Empty prefix loads server-only settings for this Node-side config. Vite
  // still exposes only VITE_* variables to browser code.
  const env = loadEnv(mode, ".", "");
  const frontendPort = envPort(env, "FRONTEND_PORT", 5199);
  const apiPort = envPort(env, "API_PORT", 9001);

  return {
    plugins: [react()],
    resolve: { alias: { "@": "/src" } },
    server: {
      port: frontendPort,
      strictPort: true,
      host: true,
      proxy: {
        "/api": {
          target: `http://127.0.0.1:${apiPort}`,
          changeOrigin: true,
        },
      },
    },
    preview: {
      port: frontendPort,
      strictPort: true,
    },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
    },
  };
});
