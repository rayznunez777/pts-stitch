/**
 * Worker in front of the Ink/Stitch container.
 *
 * Every request goes to the same named instance. That matters: the container
 * keeps the engine loaded between jobs, and importing it costs about eleven
 * seconds. Spreading requests over several instances would pay that again and
 * again.
 */
import { Container } from "@cloudflare/containers";

export class StitchContainer extends Container {
  defaultPort = 8080;
  // stay awake between jobs so the engine stays warm
  sleepAfter = "20m";

  override onStart() {
    console.log("stitch container started");
  }
  override onError(error: unknown) {
    console.log("stitch container error:", error);
  }
}

interface Env {
  STITCH: DurableObjectNamespace<StitchContainer>;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    // the browser asks permission before posting
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors() });
    }

    if (url.pathname === "/health" || url.pathname === "/digitize") {
      const container = env.STITCH.getByName("main");
      const response = await container.fetch(request);
      const headers = new Headers(response.headers);
      for (const [k, v] of Object.entries(cors())) headers.set(k, v);
      return new Response(response.body, { status: response.status, headers });
    }

    return new Response("PTS stitch service. POST /digitize, GET /health.", {
      status: 404,
      headers: cors()
    });
  }
};

function cors(): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS"
  };
}
