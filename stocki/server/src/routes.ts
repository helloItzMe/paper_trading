import { Router } from "express";
import * as q from "./queries.js";
import type { Bucket } from "./queries.js";

const router = Router();

// Every handler defaults "user" to 'ashish' -- the 7 existing positions rows
// (and all news/filings) are 'tester' mock-run data; without this default
// every real metric would start out silently showing mock numbers.
function userParam(req: { query: any }): string {
  const u = req.query.user;
  return typeof u === "string" && u.length > 0 ? u : "ashish";
}

function rangeParams(req: { query: any }): { from: string; to: string } {
  const from = typeof req.query.from === "string" ? req.query.from : new Date(0).toISOString();
  const to = typeof req.query.to === "string" ? req.query.to : new Date("2999-01-01").toISOString();
  return { from, to };
}

function bucketParam(req: { query: any }): Bucket {
  return req.query.bucket === "day" ? "day" : "15min";
}

router.get("/users", async (_req, res, next) => {
  try {
    res.json(await q.getUsers());
  } catch (e) {
    next(e);
  }
});

router.get("/summary", async (req, res, next) => {
  try {
    res.json(await q.getSummary(userParam(req)));
  } catch (e) {
    next(e);
  }
});

router.get("/summary/range", async (req, res, next) => {
  try {
    const { from, to } = rangeParams(req);
    res.json(await q.getRangeSummary(userParam(req), from, to));
  } catch (e) {
    next(e);
  }
});

router.get("/positions/live", async (req, res, next) => {
  try {
    const { from, to } = rangeParams(req);
    res.json(await q.getLivePositions(userParam(req), from, to));
  } catch (e) {
    next(e);
  }
});

router.get("/positions/closed", async (req, res, next) => {
  try {
    const { from, to } = rangeParams(req);
    res.json(await q.getClosedPositions(userParam(req), from, to));
  } catch (e) {
    next(e);
  }
});

router.get("/signals", async (req, res, next) => {
  try {
    const { from, to } = rangeParams(req);
    res.json(await q.getSignals(userParam(req), from, to));
  } catch (e) {
    next(e);
  }
});

router.get("/filings", async (req, res, next) => {
  try {
    const { from, to } = rangeParams(req);
    res.json(await q.getFilings(userParam(req), from, to));
  } catch (e) {
    next(e);
  }
});

router.get("/pending", async (req, res, next) => {
  try {
    res.json(await q.getPending(userParam(req)));
  } catch (e) {
    next(e);
  }
});

router.get("/health", async (req, res, next) => {
  try {
    res.json(await q.getHealth(userParam(req)));
  } catch (e) {
    next(e);
  }
});

router.get("/results-calendar", async (req, res, next) => {
  try {
    const from = typeof req.query.from === "string" ? req.query.from : "1970-01-01";
    const to = typeof req.query.to === "string" ? req.query.to : "2999-01-01";
    res.json(await q.getResultsCalendar(from, to));
  } catch (e) {
    next(e);
  }
});

router.get("/analysis", async (req, res, next) => {
  try {
    const date = typeof req.query.date === "string" ? req.query.date : new Date().toISOString().slice(0, 10);
    res.json(await q.getAnalysisBacktest(date));
  } catch (e) {
    next(e);
  }
});

router.get("/timeseries", async (req, res, next) => {
  try {
    const user = userParam(req);
    const { from, to } = rangeParams(req);
    const bucket = bucketParam(req);
    const [profit, newsVolume, funnel, capital, boughtSold, exitMix] = await Promise.all([
      q.getProfitSeries(user, from, to, bucket),
      q.getNewsVolumeSeries(user, from, to, bucket),
      q.getBuyFunnel(user, from, to),
      q.getCapitalSeries(user, from, to, bucket),
      q.getBoughtSoldSeries(user, from, to, bucket),
      q.getExitReasonMix(user, from, to),
    ]);
    res.json({ profit, newsVolume, funnel, capital, boughtSold, exitMix });
  } catch (e) {
    next(e);
  }
});

export default router;
