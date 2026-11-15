import { expect, it } from "vitest";
import { webReference } from "../src/markdown";

it("allows only absolute http and https references", () => {
  expect(webReference("https://example.test/a?b=1")).toBe(
    "https://example.test/a?b=1",
  );
  expect(webReference("http://example.test/")).toBe("http://example.test/");
  for (const hostile of [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    " javascript:alert(1)",
    "data:text/html,<script>1</script>",
    "vbscript:x",
    "file:///etc/passwd",
    "//example.test/x",
    "/relative",
    "",
    undefined,
    42,
    "https://example.test/" + "a".repeat(5000),
  ])
    expect(webReference(hostile)).toBeUndefined();
});
