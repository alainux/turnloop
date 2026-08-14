import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

export default defineConfig({
  root: "ui",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  resolve: { alias: { "@": resolve(import.meta.dirname, "ui/src") } },
});
