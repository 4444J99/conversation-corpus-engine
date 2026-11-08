import { bindings, defineConfig } from "cf/config";

// Values identify the deployment and pin trust; no private decryption credential
// is accepted here. Activation tooling refuses missing identity, audience or DB ID.
export default defineConfig(({ mode }) => ({
  accountId: process.env.CLOUDFLARE_ACCOUNT_ID,
  worker: {
    name: mode === "staging" ? "knovv-staging" : "knovv",
    entrypoint: "./server/worker.ts",
    compatibilityDate: "2026-11-07",
    assets: {
      runWorkerFirst: true,
      notFoundHandling: "single-page-application",
    },
    previewUrls: false,
    env: {
      ASSETS: bindings.assets(),
      DB: bindings.d1({
        name: mode === "staging" ? "knovv-staging" : "knovv",
        ...(process.env.KNOVV_D1_ID ? { id: process.env.KNOVV_D1_ID } : {}),
      }),
      BUCKET: bindings.r2({
        name: process.env.KNOVV_R2_BUCKET ?? "knovv-ciphertext",
      }),
      OWNER_EMAIL: bindings.text(process.env.KNOVV_OWNER_EMAIL ?? ""),
      OWNER_PUBLIC_KEY: bindings.text(process.env.KNOVV_OWNER_PUBLIC_KEY ?? ""),
      ACCESS_AUD: bindings.text(process.env.KNOVV_ACCESS_AUD ?? ""),
      MAX_CIPHER_BYTES: bindings.text(
        process.env.KNOVV_MAX_CIPHER_BYTES ?? "536870912",
      ),
    },
  },
}));
