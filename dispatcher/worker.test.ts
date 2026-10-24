import { describe, expect, test } from "bun:test";
import { handleRequest } from "./src/index.ts";

const env = { DISPATCH_TOKEN: "test-dispatch-secret", GITHUB_TOKEN: "test-github-secret" };

function request(token = env.DISPATCH_TOKEN, body = "{}") {
  return new Request("https://dispatcher.example/dispatch", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body,
  });
}

describe("authenticated workflow dispatcher", () => {
  test("rejects an incorrect bearer token without contacting GitHub", async () => {
    let calls = 0;
    const response = await handleRequest(request("incorrect"), env, async () => {
      calls++;
      return new Response(null, { status: 204 });
    });
    expect(response.status).toBe(401);
    expect(calls).toBe(0);
  });

  test("requires configured secrets", async () => {
    const response = await handleRequest(request(), {}, async () => new Response(null, { status: 204 }));
    expect(response.status).toBe(503);
  });

  test("does not accept GET or an unrelated path", async () => {
    for (const input of [new Request("https://dispatcher.example/dispatch"), new Request("https://dispatcher.example/other", { method: "POST" })]) {
      const response = await handleRequest(input, env, async () => {
        throw new Error("GitHub must not be called");
      });
      expect([404, 405]).toContain(response.status);
    }
  });

  test("only dispatches the fixed workflow on main", async () => {
    let destination = "";
    let options: RequestInit | undefined;
    const response = await handleRequest(request(env.DISPATCH_TOKEN, '{"ref":"untrusted","repository":"other/repository"}'), env, async (url, init) => {
      destination = String(url);
      options = init;
      return new Response(null, { status: 204 });
    });
    expect(response.status).toBe(202);
    expect(destination).toBe("https://api.github.com/repos/ryyr-ry/oci-always-free-provisioner/actions/workflows/provision.yml/dispatches");
    expect(options?.method).toBe("POST");
    expect(JSON.parse(String(options?.body))).toEqual({ ref: "main" });
    expect(new Headers(options?.headers).get("Authorization")).toBe(`Bearer ${env.GITHUB_TOKEN}`);
  });

  test("dispatches each request even if another run already exists", async () => {
    let calls = 0;
    const upstream = async () => {
      calls++;
      return new Response(null, { status: 204 });
    };
    expect((await handleRequest(request(), env, upstream)).status).toBe(202);
    expect((await handleRequest(request(), env, upstream)).status).toBe(202);
    expect(calls).toBe(2);
  });

  test("does not leak upstream bodies or credentials on errors", async () => {
    const response = await handleRequest(request(), env, async () => new Response(env.GITHUB_TOKEN, { status: 403 }));
    expect(response.status).toBe(502);
    const body = await response.text();
    expect(body).not.toContain(env.GITHUB_TOKEN);
    expect(body).not.toContain(env.DISPATCH_TOKEN);
  });

  test("reports transport failures without exposing exception details", async () => {
    const response = await handleRequest(request(), env, async () => {
      throw new Error(env.GITHUB_TOKEN);
    });
    expect(response.status).toBe(502);
    expect(await response.text()).not.toContain(env.GITHUB_TOKEN);
  });
});
