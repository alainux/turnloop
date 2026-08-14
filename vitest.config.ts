import { defineConfig } from "vitest/config";
export default defineConfig({ test: { environment: "jsdom", include: ["ui/src/**/*.test.ts", "ui/src/**/*.test.tsx"] } });
