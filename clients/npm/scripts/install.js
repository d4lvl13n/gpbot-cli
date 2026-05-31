#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const https = require("node:https");
const os = require("node:os");
const path = require("node:path");

if (process.env.GPBOT_CLI_SKIP_DOWNLOAD === "1") {
  console.error("gpbot-cli: skipping zipapp download because GPBOT_CLI_SKIP_DOWNLOAD=1");
  process.exit(0);
}

const pkg = require("../package.json");
const repo = process.env.GPBOT_CLI_REPO || "d4lvl13n/gpbot-cli";
const version = process.env.GPBOT_CLI_VERSION || pkg.version;
const vendorDir = path.join(__dirname, "..", "vendor");
const outputPath = path.join(vendorDir, "gpbot.pyz");

function platformName() {
  switch (os.platform()) {
    case "darwin":
      return "macos";
    case "linux":
      return "linux";
    default:
      throw new Error(`unsupported platform: ${os.platform()}`);
  }
}

function archName() {
  switch (os.arch()) {
    case "arm64":
      return "arm64";
    case "x64":
      return "x86_64";
    default:
      throw new Error(`unsupported architecture: ${os.arch()}`);
  }
}

function artifactUrl() {
  const artifact = `gpbot-${platformName()}-${archName()}.pyz`;
  if (version === "latest") {
    return `https://github.com/${repo}/releases/latest/download/${artifact}`;
  }
  return `https://github.com/${repo}/releases/download/gpbot-cli-v${version}/${artifact}`;
}

function download(url, destination, redirects = 0) {
  if (redirects > 5) {
    throw new Error("too many redirects while downloading gpbot CLI");
  }
  return new Promise((resolve, reject) => {
    https
      .get(url, (response) => {
        if ([301, 302, 303, 307, 308].includes(response.statusCode || 0)) {
          response.resume();
          const location = response.headers.location;
          if (!location) {
            reject(new Error(`redirect without location from ${url}`));
            return;
          }
          resolve(download(new URL(location, url).toString(), destination, redirects + 1));
          return;
        }
        if (response.statusCode !== 200) {
          response.resume();
          reject(new Error(`download failed with HTTP ${response.statusCode}: ${url}`));
          return;
        }
        const file = fs.createWriteStream(destination, { mode: 0o755 });
        response.pipe(file);
        file.on("finish", () => {
          file.close(resolve);
        });
        file.on("error", reject);
      })
      .on("error", reject);
  });
}

async function main() {
  fs.mkdirSync(vendorDir, { recursive: true });
  const url = artifactUrl();
  const tmp = `${outputPath}.tmp`;
  console.error(`gpbot-cli: downloading ${url}`);
  await download(url, tmp);
  fs.renameSync(tmp, outputPath);
  fs.chmodSync(outputPath, 0o755);
  console.error("gpbot-cli: installed gpbot zipapp");
}

main().catch((error) => {
  console.error(`gpbot-cli: ${error.message}`);
  process.exit(1);
});

