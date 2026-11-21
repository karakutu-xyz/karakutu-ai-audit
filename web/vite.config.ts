import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// KARAKUTU_API_PROXY points the dev server at another local API, for example a second
// stack on spare ports: KARAKUTU_API_PROXY=http://127.0.0.1:2230 npx vite --port 2231
const apiProxy = process.env.KARAKUTU_API_PROXY || "http://127.0.0.1:2200";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 2201,
    strictPort: true,
    proxy: { "/api": apiProxy },
  },
});
