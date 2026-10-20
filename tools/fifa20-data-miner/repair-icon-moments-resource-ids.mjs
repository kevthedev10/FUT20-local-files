import { readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const TOOL_ROOT = dirname(fileURLToPath(import.meta.url));
const CATALOG_PATH = resolve(TOOL_ROOT, "../../localfut20/players.json");
const RESOURCE_REVISION_SIZE = 0x01000000;

const catalog = JSON.parse(readFileSync(CATALOG_PATH, "utf8"));
const occupied = new Set(Object.keys(catalog).map(Number));
const replacements = [];

for (const [key, definition] of Object.entries(catalog)) {
  if (Number(definition?.rareflag) !== 84) continue;
  const oldId = Number(key);
  const baseId = Number(definition.assetId);
  if (!Number.isInteger(baseId) || baseId <= 0) {
    throw new Error(`Icon Moments ${key} has no valid base asset ID`);
  }
  // A valid special definition keeps the player's base ID in its low 24 bits.
  if ((oldId & 0x00ffffff) === baseId && oldId >= 4 * RESOURCE_REVISION_SIZE) {
    continue;
  }

  occupied.delete(oldId);
  let revision = 4;
  let newId = baseId + revision * RESOURCE_REVISION_SIZE;
  while (occupied.has(newId)) {
    revision += 1;
    newId = baseId + revision * RESOURCE_REVISION_SIZE;
  }
  occupied.add(newId);

  const legacy = new Set(
    (Array.isArray(definition.legacyResourceIds) ? definition.legacyResourceIds : [])
      .map(Number).filter((value) => Number.isInteger(value) && value > 0),
  );
  legacy.add(oldId);
  definition.legacyResourceIds = [...legacy].sort((left, right) => left - right);
  definition.resourceId = newId;
  if (definition.wefutCard && typeof definition.wefutCard === "object") {
    definition.wefutCard.resourceIdSource = "fifa-icon-moments-revision";
  }
  replacements.push([oldId, newId, definition]);
}

for (const [oldId] of replacements) delete catalog[String(oldId)];
for (const [, newId, definition] of replacements) catalog[String(newId)] = definition;

const ordered = Object.fromEntries(
  Object.entries(catalog).sort(([left], [right]) => Number(left) - Number(right)),
);
const temporary = join(dirname(CATALOG_PATH), ".players.icon-moments.tmp.json");
writeFileSync(temporary, JSON.stringify(ordered), "utf8");
renameSync(temporary, CATALOG_PATH);

console.log(`Repaired ${replacements.length} Icon Moments resource IDs.`);
