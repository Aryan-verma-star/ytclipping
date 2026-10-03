/* Node dev mirror of the Cloudflare Worker — runs proxy/worker.js's fetch
 * handler as a plain local HTTP server so the client engine can be tested
 * (and the sandbox preview can reach it via ?XTransformPort=8020).
 *
 *   node proxy/dev-server.mjs [port]      # default 8020
 *
 * Node >= 18 has the Web fetch/Request/Response globals the handler needs.
 */
import http from "node:http";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const port = Number(process.argv[2] || process.env.PORT || 8020);

const workerModule = await import(pathToFileURL(join(here, "worker.js")));
const handler = workerModule.default;

const server = http.createServer(async (req, res) => {
  try {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = chunks.length ? Buffer.concat(chunks) : undefined;

    const host = req.headers.host || `localhost:${port}`;
    const url = `http://${host}${req.url}`;
    const headers = new Headers();
    for (const [name, value] of Object.entries(req.headers)) {
      if (value == null) continue;
      if (Array.isArray(value)) value.forEach((v) => headers.append(name, v));
      else headers.set(name, String(value));
    }

    const request = new Request(url, {
      method: req.method,
      headers,
      body: ["GET", "HEAD"].includes(req.method) ? undefined : body,
      duplex: "half",
    });

    const response = await handler.fetch(request);

    res.writeHead(response.status, Object.fromEntries(response.headers.entries()));
    if (!response.body) return res.end();
    const reader = response.body.getReader();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        res.write(Buffer.from(value));
      }
    } catch (streamErr) {
      console.error("stream relay error:", streamErr);
    }
    res.end();
  } catch (err) {
    console.error("request error:", err);
    if (!res.headersSent) res.writeHead(500, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: String(err && err.message || err) }));
  }
});

server.listen(port, "0.0.0.0", () => {
  console.log(`yt-clipper cors proxy (dev) listening on http://0.0.0.0:${port}`);
  console.log(`gateway route: append ?XTransformPort=${port} to sandbox preview URLs`);
});
