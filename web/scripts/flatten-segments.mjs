// Next 16.3.8's static export on Windows writes a page's prefetch segments as `__next.verify/__PAGE__.txt`: the
// segment paths it collects carry backslashes, and convertSegmentPathToStaticExportFilename only turns `/` into `.`.
// The client router requests `__next.verify.__PAGE__.txt`, so every prefetch 404s. Copy each nested file to its
// flat name. ponytail: delete this script once Next writes flat names on Windows (on Linux they already are).
import { cpSync, readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

const out = new URL("../out/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

function walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (!statSync(path).isDirectory()) continue;
    if (name.startsWith("__next.")) flatten(dir, path);
    else walk(path);
  }
}

function flatten(parent, nested) {
  for (const name of readdirSync(nested, { recursive: true })) {
    const file = join(nested, name);
    if (statSync(file).isDirectory()) continue;
    const flat = `${relative(parent, nested)}.${name.split(sep).join(".")}`;
    cpSync(file, join(parent, flat));
  }
}

walk(out);
