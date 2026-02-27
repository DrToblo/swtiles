/**
 * SWTILES R2 Access Control Worker
 *
 * Proxies requests to a private R2 bucket, allowing only requests from
 * whitelisted origins. The R2 bucket remains private - all access goes
 * through this worker.
 */

export interface Env {
  TILES_BUCKET: R2Bucket;
  // Comma-separated list of allowed origins (e.g., "https://myapp.com,https://other.com")
  ALLOWED_ORIGINS?: string;
}

// Always allow localhost for development (any port)
const LOCALHOST_PATTERN = /^https?:\/\/localhost(:\d+)?$/;
const LOCALHOST_IP_PATTERN = /^https?:\/\/127\.0\.0\.1(:\d+)?$/;

/**
 * Check if origin is allowed
 */
function isOriginAllowed(origin: string | null, env: Env): boolean {
  if (!origin) {
    return false;
  }

  // Always allow localhost for development
  if (LOCALHOST_PATTERN.test(origin) || LOCALHOST_IP_PATTERN.test(origin)) {
    return true;
  }

  // Check against configured allowed origins
  if (env.ALLOWED_ORIGINS) {
    const allowedList = env.ALLOWED_ORIGINS.split(",").map((o) => o.trim().toLowerCase());
    const originLower = origin.toLowerCase();

    for (const allowed of allowedList) {
      // Support wildcard subdomains: *.example.com
      if (allowed.startsWith("*.")) {
        const domain = allowed.slice(2);
        if (originLower.endsWith(domain) || originLower === `https://${domain}` || originLower === `http://${domain}`) {
          return true;
        }
      } else if (originLower === allowed) {
        return true;
      }
    }
  }

  return false;
}

/**
 * Create CORS headers for allowed origin
 */
function corsHeaders(origin: string): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Range",
    "Access-Control-Expose-Headers": "Content-Length, Content-Range, Content-Type",
    "Access-Control-Max-Age": "86400",
  };
}

/**
 * Create error response
 */
function errorResponse(message: string, status: number, origin?: string): Response {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };

  if (origin) {
    Object.assign(headers, corsHeaders(origin));
  }

  return new Response(JSON.stringify({ error: message }), { status, headers });
}

/**
 * Get content type based on file extension
 */
function getContentType(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase();
  const types: Record<string, string> = {
    swtiles: "application/octet-stream",
    webp: "image/webp",
    png: "image/png",
    jpg: "image/jpeg",
    jpeg: "image/jpeg",
    avif: "image/avif",
    json: "application/json",
  };
  return types[ext || ""] || "application/octet-stream";
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const origin = request.headers.get("Origin");
    const method = request.method;

    // Check origin for all requests
    if (!isOriginAllowed(origin, env)) {
      return errorResponse("Forbidden: Origin not allowed", 403);
    }

    // Handle CORS preflight
    if (method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: corsHeaders(origin!),
      });
    }

    // Only allow GET and HEAD
    if (method !== "GET" && method !== "HEAD") {
      return errorResponse("Method not allowed", 405, origin!);
    }

    const url = new URL(request.url);
    // Remove leading slash to get R2 object key
    const key = decodeURIComponent(url.pathname.slice(1));

    if (!key) {
      // List available info at root
      return new Response(
        JSON.stringify({
          service: "SWTILES R2 Proxy",
          usage: "GET /<filename> to retrieve files from the bucket",
          example: "/Karta_10000_webp.swtiles",
        }),
        {
          status: 200,
          headers: {
            "Content-Type": "application/json",
            ...corsHeaders(origin!),
          },
        }
      );
    }

    try {
      // Check for Range header (for partial requests)
      const rangeHeader = request.headers.get("Range");
      let r2Options: R2GetOptions = {};

      if (rangeHeader) {
        // Parse Range header: "bytes=start-end"
        const match = rangeHeader.match(/bytes=(\d+)-(\d*)/);
        if (match) {
          const start = parseInt(match[1]);
          const end = match[2] ? parseInt(match[2]) : undefined;

          if (end !== undefined) {
            r2Options.range = { offset: start, length: end - start + 1 };
          } else {
            r2Options.range = { offset: start };
          }
        }
      }

      // Fetch from R2
      const object = await env.TILES_BUCKET.get(key, r2Options);

      if (!object) {
        return errorResponse("Not found", 404, origin!);
      }

      // Build response headers
      const headers: Record<string, string> = {
        "Content-Type": getContentType(key),
        "Cache-Control": "public, max-age=31536000, immutable",
        ...corsHeaders(origin!),
      };

      // Handle range response
      if (rangeHeader && r2Options.range) {
        const range = r2Options.range as { offset: number; length?: number };
        const start = range.offset;
        const length = object.size;
        const end = start + length - 1;
        const total = object.size; // Note: R2 doesn't give us total size easily for range requests

        headers["Content-Length"] = length.toString();
        headers["Content-Range"] = `bytes ${start}-${end}/*`;

        return new Response(object.body, {
          status: 206,
          headers,
        });
      }

      headers["Content-Length"] = object.size.toString();

      return new Response(object.body, {
        status: 200,
        headers,
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown error";
      console.error("R2 Error:", message);
      return errorResponse(message, 500, origin!);
    }
  },
};
