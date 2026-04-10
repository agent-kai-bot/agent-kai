export type LandingCard = {
  title: string;
  detail: string;
};

export const landingCards: LandingCard[] = [
  {
    title: "Transport",
    detail: "WebSocket protocol reused from the daemon terminal adapter",
  },
  {
    title: "Build Target",
    detail: "Static SvelteKit bundle served by FastAPI",
  },
  {
    title: "Next Slice",
    detail: "Connection flow, dashboard panels, charting, and slash palette",
  },
];
