"use strict";

const { spawnSync } = require("node:child_process");

// 服务契约单独运行（文件名不匹配 test_*.py），领域回归用 discover 自动纳入新增测试
const steps = [
  ["python3", ["-m", "unittest", "-v", "service_contract"]],
  ["python3", ["-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"]],
];
let failed = false;
for (const [cmd, args] of steps) {
  const result = spawnSync(cmd, args, { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    failed = true;
  }
}
process.exit(failed ? 1 : 0);
