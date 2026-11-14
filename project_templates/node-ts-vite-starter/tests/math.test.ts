import { describe, expect, it } from "vitest";
import { addNumbers } from "../src/math";

describe("addNumbers", () => {
  it("adds two numbers", () => {
    expect(addNumbers(2, 3)).toBe(5);
  });
});
