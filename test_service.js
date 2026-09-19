"use strict";

const { spawnSync } = require("node:child_process");

const steps = [
  ["python3", ["-m", "unittest", "-v", "service_contract"]],
  ["python3", ["-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"]],
];

for (const [command, args] of steps) {
  const result = spawnSync(command, args, { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}
