import { bindings, defineConfig } from "cf/config";
import * as entrypoint from "./src/index.ts" with { type: "cf-worker" };

export default defineConfig({
  worker: {
    name: "oci-a1-provision-dispatch",
    compatibilityDate: "2026-10-06",
    entrypoint,
    env: {
      DISPATCH_TOKEN: bindings.secret(),
      GITHUB_TOKEN: bindings.secret(),
    },
  },
});
