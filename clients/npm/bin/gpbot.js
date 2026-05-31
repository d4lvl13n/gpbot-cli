#!/usr/bin/env node
"use strict";

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const pyz = process.env.GPBOT_CLI_PYZ || path.join(__dirname, "..", "vendor", "gpbot.pyz");
const venvGpbot = path.join(__dirname, "..", "vendor", "venv", "bin", "gpbot");

let result;
if (fs.existsSync(pyz)) {
  result = spawnSync("python3", [pyz, ...process.argv.slice(2)], {
    stdio: "inherit",
  });
} else if (fs.existsSync(venvGpbot)) {
  result = spawnSync(venvGpbot, process.argv.slice(2), {
    stdio: "inherit",
  });
} else {
  console.error("gpbot: CLI payload not found. Reinstall gpbot-cli or set GPBOT_CLI_PYZ.");
  process.exit(1);
}

if (result.error) {
  console.error(`gpbot: failed to launch CLI: ${result.error.message}`);
  process.exit(1);
}

process.exit(result.status === null ? 1 : result.status);
