// Rebuild node-pty against Electron's ABI (runs as npm postinstall).
// NoDefaultCurrentDirectoryInExePath must be stripped: winpty's gyp step runs
// `cd shared && GetCommitHash.bat` (no .\ prefix), which cmd refuses to resolve
// from the current directory while that variable is set.
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const env = { ...process.env };
delete env.NoDefaultCurrentDirectoryInExePath;

// node-pty 1.1.x demands Spectre-mitigated MSVC libs (MSB8040), an optional VS
// component this machine doesn't have. Drop the flag — plain libs are fine for
// a local single-user terminal.
const ptyRoot = path.join(__dirname, "..", "node_modules", "node-pty");
for (const rel of ["binding.gyp", path.join("deps", "winpty", "src", "winpty.gyp")]) {
  const gyp = path.join(ptyRoot, rel);
  if (!fs.existsSync(gyp)) continue;
  const src = fs.readFileSync(gyp, "utf8");
  const out = src.replace(/'SpectreMitigation':\s*'Spectre'/g, "'SpectreMitigation': 'false'");
  if (out !== src) fs.writeFileSync(gyp, out);
}

const r = spawnSync("npx", ["electron-rebuild", "-f", "-w", "node-pty"], {
  stdio: "inherit",
  env,
  cwd: __dirname + "/..",
  shell: process.platform === "win32",
});
process.exit(r.status === null ? 1 : r.status);
