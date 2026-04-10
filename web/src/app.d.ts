declare global {
  interface Window {
    __KAI_DAEMON_WS__?: string;
  }
}

declare module "*.svelte" {
  import type { ComponentType } from "svelte";

  const component: ComponentType;
  export default component;
}

export {};
