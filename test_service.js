"use strict";

const { spawnSync } = require("node:child_process");

// 领域单元 -> 工作流分支 -> 双病例联调 -> HTTP 契约；任一套件失败即整体失败
const suites = ["test_domain", "test_workflow", "test_integration", "service_contract"];

for (const suite of suites) {
  console.log(`\n=== ${suite} ===`);
  const result = spawnSync(
    "python3",
    ["-m", "unittest", "-v", suite],
    { stdio: "inherit" },
  );
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

// 端到端自检（与联调用同一份剧本）
console.log("\n=== service --selftest ===");
const selftest = spawnSync("python3", ["service.py", "--selftest"], { stdio: "inherit" });
process.exit(selftest.status ?? 1);
