/// <reference types="vite/client" />

/** Injected by Vite at build time — see `define` in vite.config.ts. */
declare const __APP_VERSION__: string

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string
  readonly VITE_API_PROXY?: string
  readonly VITE_SHOW_DEMO_ACCOUNTS?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
