import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { CATALOGUE_URL, lookup, reset } from "./catalogue.ts";

const SNAPSHOT = new URL("./fixtures/models.json", import.meta.url);

// Both sides: what is loaded is remembered for the life of the process, and
// bun runs every test file in one -- so a lookup in another file, made before
// this one is reached, is otherwise still cached when these tests run.
beforeEach(reset);
afterEach(reset);

function responding(body: unknown, ok = true): typeof fetch {
  return (async (url: string | URL | Request) => {
    expect(String(url)).toBe(CATALOGUE_URL);
    return new Response(JSON.stringify(body), { status: ok ? 200 : 503 });
  }) as unknown as typeof fetch;
}

function refusing(): typeof fetch {
  return (async () => {
    throw new Error("offline");
  }) as unknown as typeof fetch;
}

describe("models.dev", () => {
  test("the snapshot baked into the image answers, without touching the network", async () => {
    const entry = await lookup("acme", { snapshot: SNAPSHOT, fetchImpl: refusing() });
    expect(entry?.npm).toBe("@ai-sdk/acme");
    expect(entry?.env).toEqual(["ACME_API_KEY"]);
  });

  test("a provider newer than the image is fetched", async () => {
    const entry = await lookup("brandnew", {
      snapshot: SNAPSHOT,
      fetchImpl: responding({ brandnew: { id: "brandnew", npm: "@ai-sdk/brandnew" } }),
    });
    expect(entry?.npm).toBe("@ai-sdk/brandnew");
  });

  test("offline, an unknown provider is simply unknown", async () => {
    expect(await lookup("nobody", { snapshot: SNAPSHOT, fetchImpl: refusing() })).toBeNull();
  });

  test("models.dev being down is not this container's problem", async () => {
    expect(
      await lookup("nobody", { snapshot: SNAPSHOT, fetchImpl: responding({}, false) }),
    ).toBeNull();
  });

  test("no snapshot at all still leaves the network", async () => {
    const entry = await lookup("acme", {
      snapshot: new URL("./fixtures/absent.json", import.meta.url),
      fetchImpl: responding({ acme: { id: "acme", npm: "@ai-sdk/acme" } }),
    });
    expect(entry?.npm).toBe("@ai-sdk/acme");
  });
});
