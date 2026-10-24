export interface Env {
  DISPATCH_TOKEN?: string;
  GITHUB_TOKEN?: string;
}

type UpstreamFetch = (url: string, options: RequestInit) => Promise<Response>;

const endpoint = "https://api.github.com/repos/ryyr-ry/oci-always-free-provisioner/actions/workflows/provision.yml/dispatches";

function reply(status: number, body: object) {
  return Response.json(body, { status, headers: { "Cache-Control": "no-store" } });
}

export async function handleRequest(request: Request, env: Env, upstream: UpstreamFetch = fetch): Promise<Response> {
  if (new URL(request.url).pathname !== "/dispatch") return reply(404, { error: "not_found" });
  if (request.method !== "POST") return reply(405, { error: "method_not_allowed" });
  if (!env.DISPATCH_TOKEN || !env.GITHUB_TOKEN) return reply(503, { error: "not_configured" });
  if (request.headers.get("Authorization") !== `Bearer ${env.DISPATCH_TOKEN}`) return reply(401, { error: "unauthorized" });

  try {
    const response = await upstream(endpoint, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "oci-a1-provision-dispatch",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref: "main" }),
      signal: AbortSignal.timeout(20_000),
    });
    if (!response.ok) return reply(502, { error: "github_dispatch_failed", upstream_status: response.status });
    return reply(202, { dispatched: true });
  } catch {
    return reply(502, { error: "github_dispatch_unavailable" });
  }
}

export default {
  fetch(request: Request, env: Env) {
    return handleRequest(request, env);
  },
} satisfies ExportedHandler<Env>;
