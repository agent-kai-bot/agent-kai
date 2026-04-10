import { Marked } from "marked";

const renderer = new Marked({
  async: false,
  breaks: true,
  gfm: true,
});

export function renderMarkdown(text: string): string {
  return renderer.parse(text) as string;
}
