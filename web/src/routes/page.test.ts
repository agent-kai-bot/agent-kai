import { landingCards } from "$lib/web-shell";

describe("KAI web shell", () => {
  it("defines the scaffold panels in the intended order", () => {
    expect(landingCards.map((card) => card.title)).toEqual([
      "Transport",
      "Build Target",
      "Next Slice",
    ]);
    expect(landingCards[0]?.detail).toMatch(/websocket protocol/i);
  });
});
