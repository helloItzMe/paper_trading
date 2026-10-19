import express from "express";
import cors from "cors";
import routes from "./routes.js";

const app = express();
app.use(cors());

// Per-request access log -- method, path, status, duration -- so server.log
// actually reflects real dashboard activity (polling, tab switches, a UI
// refresh) instead of just the one-line startup banner. Small enough to
// write directly rather than pull in a morgan dependency for it.
app.use((req, res, next) => {
  const startedAt = Date.now();
  res.on("finish", () => {
    console.log(`${req.method} ${req.originalUrl} ${res.statusCode} ${Date.now() - startedAt}ms`);
  });
  next();
});

app.use("/api", routes);

app.use((err: unknown, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
  console.error(err);
  res.status(500).json({ error: err instanceof Error ? err.message : String(err) });
});

const PORT = Number(process.env.PORT ?? 4000);
app.listen(PORT, () => {
  console.log(`stocki server listening on http://localhost:${PORT}`);
});
