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
