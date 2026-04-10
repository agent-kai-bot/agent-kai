import {
  filterPaletteItems,
  resolvePaletteQuery,
  splitSlashInput,
} from "$lib/command-palette";

describe("command palette helpers", () => {
  it("filters commands by command text or description", () => {
    expect(filterPaletteItems("schedule").map((item) => item.command)).toContain(
      "/schedule list",
    );
    expect(filterPaletteItems("current session")[0]?.command).toBe("/status");
    expect(filterPaletteItems("chart: mini")[0]?.command).toBe("/chart mini");
  });

  it("splits slash input into command and args", () => {
    expect(splitSlashInput("/schedule pause all")).toEqual({
      command: "/schedule",
      args: "pause all",
    });
    expect(splitSlashInput("status")).toEqual({
      command: "/status",
      args: "",
    });
  });

  it("resolves search terms to the first matching slash command", () => {
    expect(resolvePaletteQuery("schedule", filterPaletteItems("schedule"))).toBe(
      "/schedule list",
    );
    expect(resolvePaletteQuery("/memory compact", [])).toBe("/memory compact");
    expect(resolvePaletteQuery("memory compact", [])).toBe("memory compact");
  });
});
