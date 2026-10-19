import { Pool } from "pg";
import os from "node:os";

// Mirrors db.py's env-var contract exactly so both sides point at the same
// investment_bot database without separate configuration.
const pool = new Pool({
  host: process.env.INVESTMENT_DB_HOST ?? "localhost",
  database: process.env.INVESTMENT_DB_NAME ?? "investment_bot",
  user: process.env.INVESTMENT_DB_USER ?? os.userInfo().username,
});

export default pool;
