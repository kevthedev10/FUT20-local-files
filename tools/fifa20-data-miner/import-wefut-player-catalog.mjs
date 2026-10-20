import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const TOOL_ROOT = dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = resolve(TOOL_ROOT, "../..");
const WEFUT_ROOT = "https://wefut.com";
const GAME_YEAR = 20;
const RESOURCE_REVISION_SIZE = 0x01000000;
const PAGE_SIZE = 100;

const OUTPUT_CATALOG = join(REPOSITORY_ROOT, "localfut20/players.json");
const OUTPUT_RARITIES = join(REPOSITORY_ROOT, "runtime/mined/wefut-fifa20-rarities.json");
const OUTPUT_MANIFEST = join(REPOSITORY_ROOT, "runtime/mined/wefut-player-catalog-manifest.json");
const RARITY_TUNABLES = join(REPOSITORY_ROOT, "localfut20/contentfifa/fut/items/images/backgrounds/itemBGs/futitemraritytunables.json");
const CACHE_PATH = join(REPOSITORY_ROOT, "runtime/mined/wefut-fifa20-import-cache.json");

// WeFUT currently exposes three historical transfer rows without names. These
// two base-ID names come from the pinned native catalog that preceded this import.
const NATIVE_NAME_FALLBACKS = new Map([
  [219670, "Gonzalo Piovi"],
  [245061, "Julián López"],
]);

const RARITY_TEMPLATE_IDS = new Map([
  [33, 70], [34, 68], [37, 51], [65, 11], [75, 63], [76, 63],
  [77, 4], [79, 43], [81, 46], [82, 46], [86, 43], [87, 24],
  [89, 30], [90, 51], [91, 24], [92, 12], [93, 12], [94, 12],
  [95, 85], [96, 85], [98, 24],
]);

const FIELD_NAMES = {
  firstName: 1, lastName: 2, commonName: 3, rating: 4, dateOfBirth: 5,
  height: 6, weight: 7, position: 8, preferredFoot: 9, clubName: 10,
  leagueName: 11, nationName: 12, pace: 13, shooting: 14, passing: 15,
  dribblingFace: 16, defending: 17, physical: 18, acceleration: 19,
  agility: 20, balance: 21, jumping: 22, reactions: 23, sprintSpeed: 24,
  stamina: 25, strength: 26, aggression: 27, interceptions: 28,
  positioning: 29, vision: 30, potential: 31, ballControl: 32, crossing: 33,
  curve: 34, dribbling: 35, finishing: 36, freeKickAccuracy: 37,
  headingAccuracy: 38, longPassing: 39, longShots: 40, marking: 41,
  penalties: 42, shortPassing: 43, shotPower: 44, slidingTackle: 45,
  standingTackle: 46, volleys: 47, gkDiving: 48, gkHandling: 49,
  gkKicking: 50, gkReflexes: 51, gkSpeed: 52, gkPositioning: 53,
  weakFoot: 54, skillMoves: 55, workRateAttack: 56, workRateDefense: 57,
  rarityId: 59, teamId: 63, leagueId: 64, nationId: 65, baseId: 66,
  composure: 67, displayName: 70, cardStyle: 71, rarityClass: 72,
  rarityName: 73,
};

const ATTRIBUTE_FIELDS = [
  "acceleration", "agility", "balance", "jumping", "reactions", "sprintSpeed",
  "stamina", "strength", "aggression", "interceptions", "positioning", "vision",
  "composure", "ballControl", "crossing", "curve", "dribbling", "finishing",
  "freeKickAccuracy", "headingAccuracy", "longPassing", "longShots", "marking",
  "penalties", "shortPassing", "shotPower", "slidingTackle", "standingTackle",
  "volleys", "gkDiving", "gkHandling", "gkKicking", "gkReflexes", "gkSpeed",
  "gkPositioning",
];

function parseOptions(argv) {
  const options = { refresh: false, delayMs: 150 };
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === "--refresh") options.refresh = true;
    else if (argv[index] === "--delay-ms" && argv[index + 1]) {
      options.delayMs = Math.max(0, Number(argv[++index]));
    } else throw new Error(`Unknown or incomplete argument: ${argv[index]}`);
  }
  return options;
}

function canonicalJson(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex").toUpperCase();
}

function writeJsonAtomic(path, value) {
  const bytes = Buffer.from(canonicalJson(value), "utf8");
  const temporary = `${path}.tmp-${process.pid}`;
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(temporary, bytes);
  renameSync(temporary, path);
  return { byteLength: bytes.length, sha256: sha256(bytes) };
}

function readJson(path, fallback) {
  if (!existsSync(path)) return fallback;
  return JSON.parse(readFileSync(path, "utf8"));
}

function sleep(milliseconds) {
  return new Promise((resolveSleep) => setTimeout(resolveSleep, milliseconds));
}

async function request(url, init = {}, attempts = 4) {
  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const response = await fetch(url, {
        ...init,
        headers: {
          "User-Agent": "FIFA20-LocalFUT-WeFUT-Importer/1.0 (personal local catalog)",
          Accept: init.headers?.Accept ?? "text/html,application/json",
          ...init.headers,
        },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText}`);
      return response;
    } catch (error) {
      lastError = error;
      if (attempt < attempts) await sleep(1000 * attempt);
    }
  }
  throw lastError;
}

function numeric(row, field) {
  const value = Number(row[String(FIELD_NAMES[field])]);
  return Number.isFinite(value) ? value : 0;
}

function text(row, field) {
  return String(row[String(FIELD_NAMES[field])] ?? "").trim().replace(/\s+/g, " ");
}

function workRate(value) {
  const match = String(value ?? "").match(/>([HML])</i);
  return match ? { H: "High", M: "Medium", L: "Low" }[match[1].toUpperCase()] : "";
}

function wefutId(row) {
  const match = String(row.player_url ?? "").match(/^player\/20\/(\d+)\//);
  if (!match) throw new Error(`WeFUT row has no FIFA 20 card URL: ${row.player_url}`);
  return Number(match[1]);
}

function normalizeRow(row, playerType) {
  const commonName = text(row, "commonName");
  const firstName = text(row, "firstName");
  const lastName = text(row, "lastName");
  const name = text(row, "displayName") || commonName || `${firstName} ${lastName}`.trim();
  const attributes = Object.fromEntries(ATTRIBUTE_FIELDS.map((field) => [field, numeric(row, field)]));
  return {
    wefutId: wefutId(row),
    wefutUrl: String(row.player_url),
    playerType,
    baseId: numeric(row, "baseId"),
    firstName,
    lastName,
    commonName,
    name,
    rating: numeric(row, "rating"),
    preferredPosition: text(row, "position"),
    preferredFoot: text(row, "preferredFoot"),
    workRateAttack: workRate(row[String(FIELD_NAMES.workRateAttack)]),
    workRateDefense: workRate(row[String(FIELD_NAMES.workRateDefense)]),
    weakFoot: numeric(row, "weakFoot"),
    skillMoves: numeric(row, "skillMoves"),
    dateOfBirth: text(row, "dateOfBirth"),
    height: numeric(row, "height"),
    weight: numeric(row, "weight"),
    teamid: numeric(row, "teamId"),
    teamName: text(row, "clubName"),
    leagueId: numeric(row, "leagueId"),
    leagueName: text(row, "leagueName"),
    nation: numeric(row, "nationId"),
    nationName: text(row, "nationName"),
    rareflag: numeric(row, "rarityId"),
    rarityName: text(row, "rarityName"),
    rarityClass: text(row, "rarityClass"),
    cardStyle: text(row, "cardStyle"),
    face: ["pace", "shooting", "passing", "dribblingFace", "defending", "physical"]
      .map((field) => numeric(row, field)),
    attributes,
  };
}

async function databaseSession() {
  const response = await request(`${WEFUT_ROOT}/player-database/${GAME_YEAR}`);
  const html = await response.text();
  const setCookie = response.headers.get("set-cookie") ?? "";
  const csrf = setCookie.match(/(?:^|[,;]\s*)wf_csrf_c=([^;,]+)/)?.[1];
  if (!csrf) throw new Error("WeFUT did not issue the wf_csrf_c database cookie.");
  const filterNames = [...html.matchAll(/["']name["']\s*:\s*["']([^"']+)["']/g)]
    .map((match) => match[1]);
  return { csrf, cookie: `wf_csrf_c=${csrf}`, filterNames: [...new Set(filterNames)] };
}

async function fetchTablePage(session, playerType, start) {
  const body = new URLSearchParams({
    sEcho: "1", iColumns: "75", sColumns: "", iDisplayStart: String(start),
    iDisplayLength: String(PAGE_SIZE), sSearch: "", bRegex: "false",
    iSortingCols: "1", iSortCol_0: "4", sSortDir_0: "desc", bSortable_4: "true",
  });
  for (const name of session.filterNames) body.set(name, "");
  body.set("wf_csrf", session.csrf);
  body.set("type", playerType);
  const response = await request(`${WEFUT_ROOT}/ajax/getPlayers/${GAME_YEAR}`, {
    method: "POST",
    body,
    headers: {
      Accept: "application/json",
      Cookie: session.cookie,
      "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
      "X-Requested-With": "XMLHttpRequest",
    },
  });
  const result = await response.json();
  if (!Array.isArray(result.aaData)) throw new Error("WeFUT returned an invalid player-table response.");
  return result.aaData;
}

async function fetchAllRows(options) {
  const session = await databaseSession();
  const records = [];
  for (const playerType of ["player", "goalkeeper"]) {
    for (let start = 0; ; start += PAGE_SIZE) {
      const rows = await fetchTablePage(session, playerType, start);
      records.push(...rows.map((row) => normalizeRow(row, playerType)));
      console.log(`WeFUT ${playerType}: ${records.length.toLocaleString()} total rows fetched.`);
      if (rows.length < PAGE_SIZE) break;
      await sleep(options.delayMs);
    }
  }
  const unique = new Map();
  for (const record of records) unique.set(record.wefutId, record);
  if (unique.size !== records.length) {
    console.warn(`Removed ${(records.length - unique.size).toLocaleString()} repeated table-page rows.`);
  }
  return [...unique.values()].sort((left, right) => left.wefutId - right.wefutId);
}

async function resolveDetailResourceIds(records, options) {
  const special = records.filter(
    (record) => record.rareflag > 1 && !record.resourceId && !record.detailInspected,
  );
  let completed = 0;
  const batchSize = 4;
  for (let offset = 0; offset < special.length; offset += batchSize) {
    const batch = special.slice(offset, offset + batchSize);
    await Promise.all(batch.map(async (record) => {
      try {
        const html = await (await request(`${WEFUT_ROOT}/${record.wefutUrl}`)).text();
        const custom = html.match(/fut20\/playeravatars\/custom\/(\d+)\.png/i);
        const nativeVariant = html.match(/fut20\/playeravatars\/(\d+)\.png/i);
        if (custom) {
          record.resourceId = Number(custom[1]);
          record.resourceIdSource = "wefut-custom-avatar";
        } else if (nativeVariant && Number(nativeVariant[1]) !== record.baseId) {
          record.resourceId = Number(nativeVariant[1]);
          record.resourceIdSource = "wefut-native-avatar";
        }
      } catch (error) {
        record.detailError = String(error?.message ?? error);
        console.warn(`WeFUT detail unavailable: ${record.wefutUrl}: ${record.detailError}`);
      } finally {
        record.detailInspected = true;
      }
    }));
    completed += batch.length;
    if (completed % 100 < batchSize) {
      writeJsonAtomic(CACHE_PATH, { schemaVersion: 1, records });
      console.log(`WeFUT special definitions: ${completed.toLocaleString()}/${special.length.toLocaleString()} inspected.`);
    }
    await sleep(options.delayMs);
  }
  return records;
}

function assignResourceIds(records) {
  const used = new Set();
  const byBase = new Map();
  for (const record of records) {
    const group = byBase.get(record.baseId) ?? [];
    group.push(record);
    byBase.set(record.baseId, group);
  }

  for (const group of byBase.values()) {
    group.sort((left, right) => left.wefutId - right.wefutId);
    for (const record of group) {
      if (record.rareflag <= 1) {
        record.resourceId = record.baseId;
        record.resourceIdSource = "wefut-base-id";
      }
      // The custom-avatar number is an image key, never a FUT definition ID.
      // Putting it in the resource ID's low 24 bits makes FIFA resolve another
      // player's name/bio/position (246525 is also base player Allsop).
      if (record.rareflag === 84 && record.resourceId > 0
          && record.resourceId < RESOURCE_REVISION_SIZE) {
        record.dynamicImageResourceId = record.resourceId;
        delete record.resourceId;
        delete record.resourceIdSource;
      }
      if (record.resourceId && !used.has(record.resourceId)) used.add(record.resourceId);
      else if (record.resourceId) delete record.resourceId;
    }

    // FIFA 20's three normal Icon versions use revisions 1-3; Prime Icon
    // Moments is revision 4. Preserve the true Icon base ID in the low bits so
    // the native player database supplies the correct identity and attributes.
    for (const record of group.filter((entry) => entry.rareflag === 84 && !entry.resourceId)) {
      let revision = 4;
      while (used.has(record.baseId + revision * RESOURCE_REVISION_SIZE)) revision += 1;
      record.resourceId = record.baseId + revision * RESOURCE_REVISION_SIZE;
      record.resourceIdSource = "fifa-icon-moments-revision";
      used.add(record.resourceId);
    }

    let revision = 1;
    for (const record of group) {
      if (record.resourceId) continue;
      while (used.has(record.baseId + revision * RESOURCE_REVISION_SIZE)) revision += 1;
      record.resourceId = record.baseId + revision * RESOURCE_REVISION_SIZE;
      record.resourceIdSource = "local-revision-fallback";
      used.add(record.resourceId);
      revision += 1;
    }
  }
  return records;
}

function catalogEntry(record) {
  const tier = record.rating >= 75 ? "gold" : record.rating >= 65 ? "silver" : "bronze";
  return {
    name: record.name,
    firstName: record.firstName,
    lastName: record.lastName,
    commonName: record.commonName,
    rating: record.rating,
    preferredPosition: record.preferredPosition,
    preferredFoot: record.preferredFoot,
    workRateAttack: record.workRateAttack,
    workRateDefense: record.workRateDefense,
    weakFoot: record.weakFoot,
    skillMoves: record.skillMoves,
    dateOfBirth: record.dateOfBirth,
    height: record.height,
    weight: record.weight,
    leagueId: record.leagueId,
    leagueName: record.leagueName,
    teamid: record.teamid,
    teamName: record.teamName,
    nation: record.nation,
    nationName: record.nationName,
    assetId: record.baseId,
    resourceId: record.resourceId,
    cardsubtypeid: 0,
    rareflag: record.rareflag,
    rarityName: record.rarityName,
    rarityClass: record.rarityClass,
    tier,
    special: record.rareflag > 1,
    ...(record.dynamicImageResourceId
      ? { dynamicImageResourceId: record.dynamicImageResourceId }
      : {}),
    face: record.face,
    attributes: record.attributes,
    wefutCard: {
      id: record.wefutId,
      url: `${WEFUT_ROOT}/${record.wefutUrl}`,
      resourceIdSource: record.resourceIdSource,
      ...(record.nameSource ? { nameSource: record.nameSource } : {}),
    },
  };
}

function buildRarities(records) {
  const rarities = new Map();
  for (const record of records) {
    const existing = rarities.get(record.rareflag) ?? {
      id: record.rareflag,
      name: record.rarityName || `RARITY_${record.rareflag}`,
      className: record.rarityClass,
      cardStyle: record.cardStyle,
      cards: 0,
    };
    existing.cards += 1;
    rarities.set(record.rareflag, existing);
  }
  return [...rarities.values()].sort((left, right) => left.id - right.id);
}

function fillMissingNativeNames(records) {
  for (const record of records) {
    if (record.name) continue;
    const fallback = NATIVE_NAME_FALLBACKS.get(record.baseId);
    if (fallback) {
      record.name = fallback;
      record.nameSource = "pinned-native-catalog";
    }
  }
}

function validateRecords(records, rarities) {
  if (records.length < 15000) throw new Error(`Incomplete WeFUT catalog: only ${records.length} rows.`);
  const ids = new Set();
  for (const record of records) {
    if (!Number.isInteger(record.resourceId) || record.resourceId <= 0) {
      throw new Error(`Card ${record.wefutId} has no valid resource ID.`);
    }
    if (ids.has(record.resourceId)) throw new Error(`Duplicate resource ID ${record.resourceId}.`);
    ids.add(record.resourceId);
    if (!record.name || !record.preferredPosition || record.rating < 1 || record.rating > 99) {
      throw new Error(`Card ${record.wefutId} has incomplete core player metadata.`);
    }
  }
  const rarityIds = new Set(rarities.map((rarity) => rarity.id));
  const missingRarity = records.find((record) => !rarityIds.has(record.rareflag));
  if (missingRarity) throw new Error(`Card ${missingRarity.wefutId} has unknown rarity ${missingRarity.rareflag}.`);
}

function updateRuntimeRarityTunables(wefutRarities) {
  const document = readJson(RARITY_TUNABLES, null);
  if (!document || !Array.isArray(document.rarities)) {
    throw new Error(`Runtime rarity tunables are missing or invalid: ${RARITY_TUNABLES}`);
  }
  const byId = new Map(document.rarities.map((rarity) => [Number(rarity.id), rarity]));
  const defaultTemplate = byId.get(999);
  if (!defaultTemplate) throw new Error("Runtime rarity tunables have no DEFAULT (999) template.");

  for (const rarity of wefutRarities) {
    if (byId.has(rarity.id)) continue;
    const template = byId.get(RARITY_TEMPLATE_IDS.get(rarity.id)) ?? defaultTemplate;
    const addition = structuredClone(template);
    addition.id = rarity.id;
    addition.name = String(rarity.name || `RARITY_${rarity.id}`)
      .toUpperCase().replace(/[^A-Z0-9]+/g, "_").replace(/^_|_$/g, "");
    document.rarities.push(addition);
    byId.set(rarity.id, addition);
  }
  document.rarities.sort((left, right) => Number(left.id) - Number(right.id));
  return writeJsonAtomic(RARITY_TUNABLES, document);
}

async function main() {
  const options = parseOptions(process.argv.slice(2));
  let cache = options.refresh ? null : readJson(CACHE_PATH, null);
  let records = Array.isArray(cache?.records) ? cache.records : await fetchAllRows(options);
  writeJsonAtomic(CACHE_PATH, { schemaVersion: 1, records });
  records = await resolveDetailResourceIds(records, options);
  records = assignResourceIds(records);
  fillMissingNativeNames(records);

  const catalog = {};
  let duplicateDefinitions = 0;
  for (const record of records) {
    const key = String(record.resourceId);
    if (catalog[key]) {
      duplicateDefinitions += 1;
      continue;
    }
    catalog[key] = catalogEntry(record);
  }
  const rarities = buildRarities(records);
  validateRecords(records, rarities);
  const catalogOutput = writeJsonAtomic(OUTPUT_CATALOG, catalog);
  const rarityOutput = writeJsonAtomic(OUTPUT_RARITIES, {
    schemaVersion: 1,
    source: `${WEFUT_ROOT}/player-database/${GAME_YEAR}`,
    rarities,
  });
  const rarityTunablesOutput = updateRuntimeRarityTunables(rarities);
  const sourceCounts = Object.fromEntries(Object.entries(
    records.reduce((counts, record) => {
      counts[record.resourceIdSource] = (counts[record.resourceIdSource] ?? 0) + 1;
      return counts;
    }, {}),
  ).sort(([left], [right]) => left.localeCompare(right)));
  const manifest = {
    schemaVersion: 1,
    source: {
      description: "WeFUT FIFA 20 player database and per-card detail metadata",
      databaseUrl: `${WEFUT_ROOT}/player-database/${GAME_YEAR}`,
      retrievedAt: new Date().toISOString(),
    },
    resourceIds: {
      exact: "Base IDs and IDs exposed by WeFUT player-avatar metadata",
      fallback: "Locally assigned revision IDs preserve the FIFA base-ID bits and are explicitly counted below",
      counts: sourceCounts,
      fallbackBaseCards: records.filter(
        (record) => record.resourceIdSource === "local-revision-fallback" && record.rareflag <= 1,
      ).length,
      fallbackSpecialCards: records.filter(
        (record) => record.resourceIdSource === "local-revision-fallback" && record.rareflag > 1,
      ).length,
      detailErrors: records.filter((record) => record.detailError).length,
    },
    counts: {
      wefutRows: records.length,
      catalogCards: Object.keys(catalog).length,
      duplicateDefinitions,
      baseCards: records.filter((record) => record.rareflag <= 1).length,
      specialCards: records.filter((record) => record.rareflag > 1).length,
      rarities: rarities.length,
    },
    outputs: {
      runtimeCatalog: {
        path: relative(REPOSITORY_ROOT, OUTPUT_CATALOG).replaceAll("\\", "/"),
        ...catalogOutput,
      },
      rarities: {
        path: relative(REPOSITORY_ROOT, OUTPUT_RARITIES).replaceAll("\\", "/"),
        ...rarityOutput,
      },
      runtimeRarityTunables: {
        path: relative(REPOSITORY_ROOT, RARITY_TUNABLES).replaceAll("\\", "/"),
        ...rarityTunablesOutput,
      },
    },
    generator: relative(REPOSITORY_ROOT, fileURLToPath(import.meta.url)).replaceAll("\\", "/"),
  };
  writeJsonAtomic(OUTPUT_MANIFEST, manifest);
  writeJsonAtomic(CACHE_PATH, { schemaVersion: 1, records });

  console.log(`Imported ${Object.keys(catalog).length.toLocaleString()} FIFA 20 cards across ${rarities.length} rarities.`);
  console.log(`Resource ID provenance: ${JSON.stringify(sourceCounts)}`);
}

await main();
