// IDs are assigned to parsed headings, including nested headings; code blocks and raw HTML
// never become navigation targets. The reader builds its TOC from the rendered headings.
export function headingIds() {
  return (tree: any) => {
    let count = 0;
    function visit(node: any) {
      if (node.type === "heading") {
        node.data ??= {};
        node.data.hProperties ??= {};
        node.data.hProperties.id = "knovv-section-" + count++;
      }
      for (const child of node.children ?? []) visit(child);
    }
    visit(tree);
  };
}
// Only absolute http(s) URLs may become external links; javascript:, data: and
// other schemes written by an agent or imported capture are never rendered as anchors.
export function webReference(value: unknown): string | undefined {
  if (typeof value !== "string" || value.length > 4096) return undefined;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:"
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
}
