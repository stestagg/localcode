import { describe, expect, test } from "bun:test";

import type { CatalogueEntry } from "./catalogue.ts";
import { LlmError } from "./errors.ts";
import {
  COMPATIBLE,
  NO_KEY,
  apiKeyFor,
  credential,
  factoryFrom,
  packageFor,
  importProvider,
  packageName,
} from "./provider.ts";
import { type Settings, CONFIG_ENV, AUTH_ENV, loadSettings } from "./settings.ts";

function settings(config: unknown, auth: unknown = {}): Settings {
  return loadSettings({
    [CONFIG_ENV]: JSON.stringify(config),
    [AUTH_ENV]: JSON.stringify(auth),
  });
}

const ACME: CatalogueEntry = { id: "acme", npm: "@ai-sdk/acme", env: ["ACME_API_KEY"] };

describe("which package to install", () => {
  test("a subpath is installed as its package", () => {
    expect(packageName("@ai-sdk/google-vertex/anthropic")).toBe("@ai-sdk/google-vertex");
    expect(packageName("@ai-sdk/anthropic")).toBe("@ai-sdk/anthropic");
    expect(packageName("ai-gateway-provider")).toBe("ai-gateway-provider");
  });
});

describe("which package implements a provider", () => {
  test("the config's own npm wins, because it is what opencode reads too", () => {
    const config = { provider: { acme: { npm: "@vendor/theirs" } } };
    expect(packageFor(settings(config), "acme", ACME)).toBe("@vendor/theirs");
  });

  test("otherwise models.dev says", () => {
    expect(packageFor(settings({}), "acme", ACME)).toBe("@ai-sdk/acme");
  });

  test("an address on its own means the compatible protocol", () => {
    const config = { provider: { acme: { options: { baseURL: "http://x/v1" } } } };
    expect(packageFor(settings(config), "acme", null)).toBe(COMPATIBLE);
  });

  test("neither leaves nothing to guess from, and it names both fixes", () => {
    expect(() => packageFor(settings({}), "acme", null)).toThrow(LlmError);
    expect(() => packageFor(settings({}), "acme", null)).toThrow(/provider\.acme\.npm/);
    expect(() => packageFor(settings({}), "acme", null)).toThrow(/baseURL/);
  });
});

describe("the stored credential", () => {
  test("the shapes opencode is known to write", () => {
    expect(credential({ acme: { type: "api", key: "sk-1" } }, "acme")).toBe("sk-1");
    expect(credential({ acme: { type: "oauth", access: "at-1", refresh: "rt" } }, "acme")).toBe(
      "at-1",
    );
    expect(credential({ acme: { token: "tk-1" } }, "acme")).toBe("tk-1");
  });

  test("a field name opencode has not invented yet is still found", () => {
    expect(credential({ acme: { type: "api", secret: "sk-2" } }, "acme")).toBe("sk-2");
  });

  test("what a credential is made of is never mistaken for the credential", () => {
    expect(credential({ acme: { type: "api" } }, "acme")).toBeUndefined();
    expect(credential({ acme: { type: "oauth", refresh: "rt" } }, "acme")).toBeUndefined();
  });

  test("no credential for this provider at all", () => {
    expect(credential({}, "acme")).toBeUndefined();
    expect(credential({ acme: "not-an-object" }, "acme")).toBeUndefined();
  });
});

describe("the key to present", () => {
  test("an explicit apiKey in the config wins over everything", () => {
    const config = { provider: { acme: { options: { apiKey: "sk-config" } } } };
    const auth = { acme: { key: "sk-stored" } };
    expect(apiKeyFor(settings(config, auth), "acme", ACME, { ACME_API_KEY: "sk-env" })).toBe(
      "sk-config",
    );
  });

  test("a {env:} reference is a reference to a key, not a key", () => {
    const config = { provider: { acme: { options: { apiKey: "{env:MY_KEY}" } } } };
    expect(apiKeyFor(settings(config), "acme", ACME, { MY_KEY: "sk-env" })).toBe("sk-env");
  });

  test("an empty apiKey is an answer: this server wants none", () => {
    const config = { provider: { acme: { options: { apiKey: "" } } } };
    expect(apiKeyFor(settings(config), "acme", ACME, {})).toBe(NO_KEY);
  });

  test("then the credential store", () => {
    const auth = { acme: { key: "sk-stored" } };
    expect(apiKeyFor(settings({}, auth), "acme", ACME, { ACME_API_KEY: "sk-env" })).toBe(
      "sk-stored",
    );
  });

  test("then whichever variable models.dev says this provider answers to", () => {
    expect(apiKeyFor(settings({}), "acme", ACME, { ACME_API_KEY: "sk-env" })).toBe("sk-env");
  });

  test("nothing anywhere leaves it to the package's own lookup", () => {
    expect(apiKeyFor(settings({}), "acme", ACME, {})).toBeUndefined();
    expect(apiKeyFor(settings({}), "acme", null, {})).toBeUndefined();
  });
});

describe("a provider package's factory", () => {
  test("the one create* export is the one used", () => {
    const factory = () => "built";
    expect(factoryFrom({ createAcme: factory, acme: () => 0 }, "@ai-sdk/acme")).toBe(factory);
  });

  test("where a package exports an alias too, the general name is the shorter", () => {
    const general = () => "general";
    const alias = () => "alias";
    expect(
      factoryFrom({ createAcmeGenerativeAI: alias, createAcme: general }, "@ai-sdk/acme"),
    ).toBe(general);
  });

  test("a package with no factory is not an AI SDK provider, and is told so", () => {
    expect(() => factoryFrom({ acme: () => 0 }, "@vendor/theirs")).toThrow(LlmError);
    expect(() => factoryFrom({}, "@vendor/theirs")).toThrow(/@vendor\/theirs/);
  });
});

describe("loading a provider package", () => {
  test("one the image was built with is loaded without reaching for npm", async () => {
    const module = await importProvider(COMPATIBLE);
    expect(typeof factoryFrom(module, COMPATIBLE)).toBe("function");
  });

  test("one that does not exist anywhere says so in a line", async () => {
    await expect(importProvider("@localcode/nothing-implements-this")).rejects.toThrow(LlmError);
  });
});
