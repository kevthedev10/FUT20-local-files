import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

const projectRoot = path.resolve(import.meta.dirname, "../..");
const tableRoot = path.join(projectRoot, "runtime", "mined", "native-fifa20", "tables", "fifa_ng_db");
const catalogPath = path.join(projectRoot, "localfut20", "players.json");
const manifestPath = path.join(projectRoot, "runtime", "mined", "native-fifa20", "catalog-overlay-manifest.json");

const readJson = (name) => JSON.parse(fs.readFileSync(path.join(tableRoot, name), "utf8"));
const indexBy = (rows, key) => new Map(rows.map((row) => [Number(row[key]), row]));
const sha256 = (file) => crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");

const nativePlayers = indexBy(readJson("players.json"), "playerid");
const names = indexBy(readJson("playernames.json"), "nameid");
const teams = indexBy(readJson("teams.json"), "teamid");
const leagues = indexBy(readJson("leagues.json"), "leagueid");
const nations = indexBy(readJson("nations.json"), "nationid");
const playerTeams = indexBy(readJson("teamplayerlinks.json"), "playerid");
const teamLeagues = indexBy(readJson("leagueteamlinks.json"), "teamid");
const catalog = JSON.parse(fs.readFileSync(catalogPath, "utf8"));

const attributeMap = {
  acceleration: "acceleration", agility: "agility", balance: "balance", jumping: "jumping",
  reactions: "reactions", sprintSpeed: "sprintspeed", stamina: "stamina", strength: "strength",
  aggression: "aggression", interceptions: "interceptions", positioning: "positioning", vision: "vision",
  composure: "composure", ballControl: "ballcontrol", crossing: "crossing", curve: "curve",
  dribbling: "dribbling", finishing: "finishing", freeKickAccuracy: "freekickaccuracy",
  headingAccuracy: "headingaccuracy", longPassing: "longpassing", longShots: "longshots",
  marking: "marking", penalties: "penalties", shortPassing: "shortpassing", shotPower: "shotpower",
  slidingTackle: "slidingtackle", standingTackle: "standingtackle", volleys: "volleys",
  gkDiving: "gkdiving", gkHandling: "gkhandling", gkKicking: "gkkicking",
  gkReflexes: "gkreflexes", gkPositioning: "gkpositioning",
};

const resolvedName = (id) => names.get(Number(id))?.name || "";
let matchedCards = 0;
let overlaidBaseCards = 0;
const matchedNativePlayers = new Set();

for (const card of Object.values(catalog)) {
  if (!card || typeof card !== "object") continue;
  const assetId = Number(card.assetId || card.resourceId || 0);
  const native = nativePlayers.get(assetId);
  if (!native) continue;
  matchedCards += 1;
  matchedNativePlayers.add(assetId);
  card.nativeDatabase = {
    table: "fifa_ng_db.players",
    playerId: assetId,
    headAssetId: Number(native.headassetid || assetId),
  };

  // FUT special cards deliberately retain their upgraded rating and face
  // attributes. The supplied fifa_ng_db contains career/base players, not the
  // cards_ng_db FUT revisions that define those upgrades.
  if (card.special || Number(card.resourceId) !== assetId) continue;
  overlaidBaseCards += 1;
  const teamLink = playerTeams.get(assetId);
  const team = teamLink ? teams.get(Number(teamLink.teamid)) : undefined;
  const leagueLink = teamLink ? teamLeagues.get(Number(teamLink.teamid)) : undefined;
  const league = leagueLink ? leagues.get(Number(leagueLink.leagueid)) : undefined;
  const nation = nations.get(Number(native.nationality));
  const firstName = resolvedName(native.firstnameid);
  const lastName = resolvedName(native.lastnameid);
  const commonName = resolvedName(native.commonnameid);

  card.firstName = firstName;
  card.lastName = lastName;
  card.commonName = commonName;
  card.name = commonName || lastName || firstName || card.name;
  card.rating = Number(native.overallrating);
  card.nation = Number(native.nationality);
  card.nationName = nation?.nationname || card.nationName;
  card.height = Number(native.height);
  card.weight = Number(native.weight);
  card.preferredFoot = Number(native.preferredfoot) === 2 ? "Left" : "Right";
  card.weakFoot = Number(native.weakfootabilitytypecode);
  if (teamLink) card.teamid = Number(teamLink.teamid);
  if (team) card.teamName = team.teamname;
  if (leagueLink) card.leagueId = Number(leagueLink.leagueid);
  if (league) card.leagueName = league.leaguename;
  card.attributes = card.attributes || {};
  for (const [target, source] of Object.entries(attributeMap)) {
    if (native[source] !== undefined) card.attributes[target] = Number(native[source]);
  }
  card.attributes.gkSpeed = 0;
}

const temporary = `${catalogPath}.native.tmp`;
fs.writeFileSync(temporary, `${JSON.stringify(catalog, null, 2)}\n`);
fs.renameSync(temporary, catalogPath);

const sourceDb = path.join(projectRoot, "runtime", "mined", "native-fifa20-input", "fifa_ng_db.db");
const sourceMeta = path.join(projectRoot, "runtime", "mined", "native-fifa20-input", "fifa_ng_db-meta.xml");
const manifest = {
  importedAt: new Date().toISOString(),
  strategy: "native fifa_ng_db base overlay plus existing FUT special revisions",
  nativePlayerRows: nativePlayers.size,
  catalogCardsMatched: matchedCards,
  distinctNativePlayersMatched: matchedNativePlayers.size,
  baseCardsOverlaid: overlaidBaseCards,
  specialCardsPreserved: matchedCards - overlaidBaseCards,
  inputSha256: { database: sha256(sourceDb), metadata: sha256(sourceMeta) },
  outputSha256: sha256(catalogPath),
};
fs.mkdirSync(path.dirname(manifestPath), { recursive: true });
fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
console.log(JSON.stringify(manifest, null, 2));
