#!/usr/bin/env node
"use strict";

const { spawnSync } = require("node:child_process");
const path = require("node:path");

const pyz = process.env.GPBOT_CLI_PYZ || path.join(__dirname, "..", "vendor", "gpbot.pyz");

const result = spawnSync("python3", [pyz, ...process.argv.slice(2)], {
  stdio: "inherit",
});

if (result.error) {
  console.error(`gpbot: failed to launch python3: ${result.error.message}`);
  process.exit(1);
}

process.exit(result.status === null ? 1 : result.status);

