#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const https = require("node:https");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

if (process.env.GPBOT_CLI_SKIP_DOWNLOAD === "1") {
  console.error("gpbot-cli: skipping zipapp download because GPBOT_CLI_SKIP_DOWNLOAD=1");
  process.exit(0);
}

const pkg = require("../package.json");
const repo = process.env.GPBOT_CLI_REPO || "d4lvl13n/gpbot-cli";
const version = process.env.GPBOT_CLI_VERSION || pkg.version;
const vendorDir = path.join(__dirname, "..", "vendor");
const outputPath = path.join(vendorDir, "gpbot.pyz");
const venvDir = path.join(vendorDir, "venv");

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
      .get(url, { headers: { "User-Agent": "gpbot-cli-installer" } }, (response) => {
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

function fetchJson(url, redirects = 0) {
  if (redirects > 5) {
    throw new Error("too many redirects while fetching GitHub release metadata");
  }
  return new Promise((resolve, reject) => {
    https
      .get(
        url,
        {
          headers: {
            Accept: "application/vnd.github+json",
            "User-Agent": "gpbot-cli-installer",
          },
        },
        (response) => {
          if ([301, 302, 303, 307, 308].includes(response.statusCode || 0)) {
            response.resume();
            const location = response.headers.location;
            if (!location) {
              reject(new Error(`redirect without location from ${url}`));
              return;
            }
            resolve(fetchJson(new URL(location, url).toString(), redirects + 1));
            return;
          }
          let body = "";
          response.setEncoding("utf8");
          response.on("data", (chunk) => {
            body += chunk;
          });
          response.on("end", () => {
            if (response.statusCode !== 200) {
              reject(new Error(`GitHub release metadata failed with HTTP ${response.statusCode}: ${url}`));
              return;
            }
            try {
              resolve(JSON.parse(body));
            } catch (error) {
              reject(error);
            }
          });
        },
      )
      .on("error", reject);
  });
}

async function findWheelUrl() {
  const apiUrl =
    version === "latest"
      ? `https://api.github.com/repos/${repo}/releases/latest`
      : `https://api.github.com/repos/${repo}/releases/tags/gpbot-cli-v${version}`;
  const release = await fetchJson(apiUrl);
  const asset = (release.assets || []).find((candidate) =>
    /^gpbot_cli-[^-]+-py3-none-any\.whl$/.test(candidate.name || ""),
  );
  if (!asset || !asset.browser_download_url) {
    throw new Error("no gpbot_cli wheel found in release assets");
  }
  return asset.browser_download_url;
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { stdio: "inherit", ...options });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(" ")} exited with ${result.status}`);
  }
}

function installWheel(wheelUrl) {
  fs.rmSync(venvDir, { recursive: true, force: true });
  run("python3", ["-m", "venv", venvDir]);
  const python = path.join(venvDir, "bin", "python");
  run(python, ["-m", "pip", "install", "--upgrade", "pip"], { stdio: "ignore" });
  run(python, ["-m", "pip", "install", wheelUrl]);
}

async function main() {
  fs.mkdirSync(vendorDir, { recursive: true });
  const url = artifactUrl();
  const tmp = `${outputPath}.tmp`;
  fs.rmSync(tmp, { force: true });

  if (process.env.GPBOT_CLI_FORCE_WHEEL !== "1") {
    try {
      console.error(`gpbot-cli: downloading ${url}`);
      await download(url, tmp);
      fs.renameSync(tmp, outputPath);
      fs.chmodSync(outputPath, 0o755);
      fs.rmSync(venvDir, { recursive: true, force: true });
      console.error("gpbot-cli: installed gpbot zipapp");
      return;
    } catch (error) {
      fs.rmSync(tmp, { force: true });
      console.error(`gpbot-cli: ${error.message}`);
      console.error("gpbot-cli: falling back to pure Python wheel install");
    }
  }

  const wheelUrl = await findWheelUrl();
  fs.rmSync(outputPath, { force: true });
  installWheel(wheelUrl);
  console.error("gpbot-cli: installed gpbot wheel");
}

main().catch((error) => {
  console.error(`gpbot-cli: ${error.message}`);
  process.exit(1);
});
