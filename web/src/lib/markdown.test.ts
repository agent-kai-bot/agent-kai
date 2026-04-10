import { renderMarkdown } from "$lib/markdown";

describe("renderMarkdown", () => {
  it("renders headings, lists, and fenced code", () => {
    const html = renderMarkdown("## Signal\n\n- BTC\n\n```python\nprint('hi')\n```");

    expect(html).toContain("<h2>Signal</h2>");
    expect(html).toContain("<li>BTC</li>");
    expect(html).toContain("<code class=\"language-python\">");
  });
});
