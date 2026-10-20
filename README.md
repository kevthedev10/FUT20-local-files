# FIFA 20 Local FUT

### 1.0.32 Live Objective mapping + in-session refresh test

This build is based directly on 1.0.31 and keeps its curated SBC, Squad Battles, market and Store work.

Objective mapping is explicit:

- Draft Debut = settle 1 Offline Draft match (`draft.matches`)
- Draft Victory = WIN 1 Offline Draft match (`draft.wins`)
- Draft Run = settle 4 Offline Draft matches (`draft.matches`)
- Squad Battles objectives read only `squad_battles.matches` / `squad_battles.wins`
- ordinary FUT objectives read only the ordinary FUT aggregate (generic/Rivals/Champions), never Draft or Squad Battles

The live-refresh experiment also re-announces the existing Blaze LoginSession once after an Objective revision changes, followed by the normal UserSessions update pulses. It does not auto-claim rewards or force a FUT logout.

See `LIVE_OBJECTIVES_MAPPED_SOURCE_1.0.32_NOTES.md`.

### 1.0.31 Curated SBCs, featured Squad Battles and objective mode isolation

- Removes the incomplete 598-group recovered SBC import. The active catalogue
  now keeps the current build's Basic groups, its Advanced groups except the
  National Pride mini-challenge, the Bronze/Silver/Gold Upgrades, the two
  verified live/player SBCs, and the stable older extra groups.
- Adds repeatable Icon Moments and FUT Birthday Player Pick SBCs backed by the
  native `miscPlayerPick` token lifecycle, with explicit nonblank reward names.
- Normalizes every active SBC badge to the retail-calibrated 375x520 canvas so
  the artwork has consistent size and sits directly above the tile shadow.
- Activates a custom Featured All-Stars squad alongside the featured TOTW,
  preserves its identity through match launch, rotates real club badges/kits
  with each opponent refresh, and promotes several real cards to special
  versions in every round.
- Keeps retail-like DNF handling: quitting a Squad Battles match records the
  match/loss but awards zero points and zero coins.
- Restores the proven fast physical Transfer Market population (185 player
  listings plus 15 managers) and migrates oversized existing CPU markets down
  safely. Exact-card Compare Price behavior is unchanged.
- Hides Gold Players, Premium Gold Players and Prime Gold Players packs from
  the coin store while still allowing earned reward copies in My Packs.
- Separates Season Objectives by settled mode: Draft advances Draft only,
  Squad Battles advances Squad Battles only, and ordinary FUT objectives use
  ordinary FUT/Rivals/Champions results rather than the global club record.
- See `SQBT_SBC_CURATED_SOURCE_1.0.31_NOTES.md`.

### 1.0.30 Market, Squad Battles and Objectives refresh

- Restores a physical 400-player Transfer Market instead of repeating a small
  pool across hundreds of thousands of virtual pages. Every search page now
  contains distinct trade IDs.
- Compare Price now filters by the selected item's exact `resourceId` /
  `definitionId`, so a special card cannot be mixed with the base version of
  the same footballer and matches can be found beyond the current page.
- Keeps the advanced Squad Battles systems from 1.0.28—persistent weekly
  points, ranks, four-opponent round refreshes, the featured TOTW squad,
  40-match accounting and weekly rewards—while restoring FIFA 20's proven
  native hub/opponent summary schema and stable 0–3 carousel slot IDs.
- Restores the complete `homeSquad` match-launch alias required by FIFA's
  second-stage game-session handshake, including the user's manager, kits and
  stadium data.
- Manual Season Objective claims now advance the SCMP revision and immediately
  return a full replacement campaign envelope, including redeemed state, so
  the Objectives frontend can update without a FUT logout.
- Preserves the native Player Pick Store test implementation from 1.0.29.
- See `MARKET_SQBT_OBJECTIVES_REFRESH_SOURCE_1.0.30_NOTES.md`.

### 1.0.29 Store Player Pick test

- Adds three temporary Special-store test articles backed by the native FIFA 20 `miscPlayerPick` lifecycle: 1 of 3 Promo (84+), 1 of 3 TOTS (88+), and 1 of 3 Icon Moments (90+).
- Buying a test article grants resource `5004241` as an unassigned Player Pick token; redeeming that token uses `/item/nontargeted`, and only the selected candidate becomes an owned unassigned player.
- The Store tile still uses FIFA 20's native Special Pack shell (`packAssetId=4`) so the test article can be selected without inventing a new pack asset.
- This is intentionally a tester build for validating the imported Player Pick script in the retail client.

### 1.0.28 Retail systems integration

- Preserves the corrected FIFA 20 TOTW/TOTW Moments archive from 1.0.27.
- Adds FIFA 20's native misc-item Player Pick lifecycle: persistent reward
  tokens, temporary choices, duplicate flags, one selected unassigned card and
  idempotent retry handling.
- Expands the SCMP campaign envelope and diagnostics so post-match category
  details carry the current campaign revision and the retail cache-invalidation
  fields without forcing a FUT logout or auto-claiming rewards.
- Keeps all 598 recovered playable SBC groups and 1,224 mini-challenges. Merges
  191 additional group reward labels from the bundled FUTBIN archive, preserves
  exact player-card rewards, and adds native names/assets for the recovered pack
  and Player Pick reward types.
- Repositions all generated SBC badges with a taller transparent texture socket
  and cache key v9 so the badge sits above the native shadow rather than at the
  top of the tile.
- Advertises a 400,000-card transfer catalogue, provides populated deep pages,
  and keeps exact Compare Price searches physical/read-only.
- Adds persistent Squad Battles points/ranks/rewards, rotating four-opponent
  rounds through the 40 regular matches, a separately settled featured TOTW
  match, and Top 100 data.
- Full regression result: 133 tests passed.
- See `RETAIL_SYSTEMS_COMPLETE_SOURCE_1.0.28_NOTES.md`.

### 1.0.27 Live Season Objectives full-campaign refresh test

- The latest tester trace proves progress persistence is correct and that FIFA requests fresh category details after the match, but keeps rendering the active-campaign snapshot loaded at FUT authentication.
- A changed objective revision now makes the first category-details response carry the complete active campaign as a schema-compatible superset, while keeping objective rewards manual/retail-like.
- Adds `[SCMP-LIVE]` progress diagnostics and explicitly acknowledges `/ut/delete/auth`.
- See `LIVE_OBJECTIVES_FULL_CAMPAIGN_REFRESH_TEST_SOURCE_1.0.27_NOTES.md`.

### 1.0.24 TOTW selector duplicate cleanup and SBC image sizing

- Removed the two excess native selector cards that appeared as `WEEK 46/47`
  and `WEEK 47/47` while both showing Team of the Week 45. The history route
  now stops at Week 43 and leaves FIFA 20's two native live/current slots to
  represent the real Weeks 44 and 45.
- Weeks 33–37 remain explicit unavailable historical slots, so the real resumed
  TOTW numbering is still preserved and no fake squads are introduced.
- Stale requests for selector IDs 46/47 no longer alias the latest Week 45
  squad.
- Rebuilt all active SBC set images after trimming source transparency first.
  This makes player/upgrade/group badges larger and more consistent while
  retaining the native 375×347 texture canvas.
- Bumped SBC set image IDs to `-localfut20-sbc-v8` so the game discards cached
  v7 artwork.
- See `TOTW_SELECTOR_SBC_ART_FIX_SOURCE_1.0.24_NOTES.md`.

### 1.0.22 Complete recovered SBC catalogue, historical TOTW identity, and live Objectives

- Replaced the 12-group interim catalogue with all 598 playable FIFA 20 SBC
  groups recovered from the supplied 608-record archive. The ten omitted index
  records have no challenge block in the archive (duplicate, kit, or retired
  empty pages); they are deliberately not rendered as blank SBCs.
- Loaded all 1,224 recovered mini-challenges. Names, descriptions, repeatable
  state, source requirements and source rewards are retained, while 4,038 of
  4,609 requirement rows are normalized into native LocalFUT validation rules.
  The 571 source rules that cannot be represented safely remain visible in the
  audit payload and are not replaced with false requirements.
- Player SBCs now return the real matching FIFA 20 promo-card identity and a
  complete native preview record (name, rating, position, resource id and card
  data), fixing the blank Ricardo Quaresma-style reward frame.
- Generated a nonblank 375x347 badge canvas for every playable group, retained
  the verified original art where available, and lowered the visible badge to
  sit immediately above the native carousel shadow.
- TOTW archive entries now carry separate archive index, release type, actual
  historical week, Moments number and display label metadata. Moments 1–6 map
  to releases 27–32, weeks 33–37 remain absent, and regular TOTW resumes at
  real week 38 through week 45.
- Objective campaign/detail responses now carry revision and last-updated data
  with no-cache headers, complementing the existing match-end live progress
  response. Claimed season rewards remain persisted and cannot be claimed a
  second time after re-entering Ultimate Team.
- See `FULL_SBC_CATALOG_TOTW_LIVE_SOURCE_1.0.22_NOTES.md` for coverage and
  verification details. FIFA itself was not launched.

### 1.0.21 Live Objectives, verified SBC internals, and complete FIFA 20 TOTW run

- Objective XP and revision data now travel with normal user and match-end
  responses, so the native menus can refresh progress without requiring a new
  server process. Season-level claims remain transactionally persisted and a
  repeated claim after reopening Ultimate Team cannot grant its reward twice.
- Extended the playable, source-verified SBC catalogue to 12 groups and 33
  individual challenges. This includes the exact four internal challenges of
  Four Leaf Clover, its real requirements and pack rewards, and the exact
  Flashback Ricardo Quaresma player card as an untradeable group reward.
- Bundled a 946-entry FUTBIN group index for provenance and future recovery,
  but only groups whose individual requirements and rewards were independently
  recovered are exposed in game. There are no blank or invented mini-challenges.
- Every active SBC group uses its original FIFA 20/FUTBIN artwork on a tested
  375x237 transparent canvas. Alpha-bound tests enforce the retail-style size,
  right-half placement and vertical baseline that were missing before.
- The featured-squad archive now contains all 40 authentic FIFA 20 entries:
  regular TOTW 1–26 and 38–45 plus the six separately named TOTW Moments
  squads. It contains 904 cards and never invents regular TOTW 27–37.
- See `LIVE_OBJECTIVES_REAL_SBC_TOTW_SOURCE_1.0.21_NOTES.md` for source and
  verification details. Draft difficulty and coins are unchanged.

### 1.0.20 Published TOTW lineups and SBC badge layout

- Rebuilt the bundled 38-squad archive from FIFAUTeam's `OFFICIAL SQUAD`
  sections, cross-checking identities, printed ratings and starting elevens
  against FIFPlay's weekly articles. No predicted squads or invented regular
  TOTW 27–37 are included.
- The featured-squad detail route now uses the published starting eleven rather
  than picking the eleven highest-rated cards. Formation IDs are inferred from
  the published position list because the referenced articles do not document
  a native FIFA formation ID; they are not claimed as verified originals.
- Enlarged and horizontally inset the visible SBC group artwork, preserving
  its existing design, and refreshed the image URL cache key. Actual in-game
  scale remains unverified because this release was tested source-only.
- See `TOTW_OFFICIAL_SBC_SCALE_SOURCE_1.0.20_NOTES.md` for discrepancies and
  the verification scope. Existing Draft, coins and other modes are unchanged.

### 1.0.19 Authentic FIFA 20 TOTW archive and SBC artwork

- Replaced the malformed nested featured-squad history that FIFA rendered as
  124 empty weeks. The archive now exposes 38 actually published FIFA 20
  squads through the native ID-list/detail contract: TOTW 1–26, six TOTW
  Moments squads, and TOTW 38–43.
- Bundled 858 real FIFA 20 catalogue cards across those squads. Each archive
  selection returns one complete, positionally valid native squad instead of
  the old repeated history wrapper.
- Replaced the synthetic square badges for the permanent foundation/advanced
  SBC groups with their published FIFA 20 portrait artwork. Remaining local
  promo/upgrade badges now use the same portrait proportions, and the `v3`
  image IDs force FIFA to discard its stale cache.
- Full details and source provenance are in
  `TOTW_ARCHIVE_SBC_ART_COMPLETE_SOURCE_1.0.19_NOTES.md`. Offline Draft,
  rewards, Objectives, pack exclusions, Club safety and leaderboard gating are
  unchanged.

### 1.0.14 Native Draft round difficulty and Leaderboards feature gate

- Replaced the ineffective Draft difficulty fields at the state-response root
  with FIFA 20's native `roundsInfo` record. Retail `CardsDLL` parses the
  selected level from `roundsInfo[].difficultyName`, alongside the current
  round, opponent id/rating, score and penalty-score fields. The record now
  advances with the Draft run and remains empty until a difficulty is chosen.
- Disabled the unsupported general Leaderboards entry through the retail
  `usLeaderboardAvailable` setting and matching FUT Hub flag. Its endpoint
  remains a disabled fallback, while the separate Squad Battles, Rivals and
  Champions leaderboard routes are unchanged.
- Draft coin and pack reward calculation, award formatting and claim handling
  are unchanged from 1.0.13, where the supplied screenshot confirmed the
  750-coin display was fixed.
- This is source-only work verified statically against the supplied log,
  minidumps and installed retail binaries, plus automated server tests. FIFA
  itself was not launched, as requested.

### 1.0.13 Draft native handoff attempt (superseded)

- Attempted to carry Draft difficulty through response-root fields. The
  supplied follow-up log and screenshots proved FIFA ignored those fields;
  1.0.14 replaces them with the record its retail parser actually consumes.
- Restored the native Draft award wire format verified from the supplied retail
  screenshots: numeric type 2 for coins, numeric type 0 for packs, and the real
  pack id in `halId`. This replaces the 1.0.12 string awards that produced a
  blank screen and fixes the old pack row being interpreted as `1 Coins`.
- General Leaderboards returned a non-success `FEATURE_DISABLED` response.
  The supplied follow-up proved that this endpoint-only approach still
  crashed; 1.0.14 now prevents navigation using the retail feature gate.
- The game installation and player profile are not modified. This is the same
  complete-source layout with Launch-LocalFUT, launcher settings, certificates,
  the x12 Position Modifiers Pack, Miz Special Pack and Wez Pack preserved.

### 1.0.12 Leaderboards and Draft reward wire correction

- General Leaderboards no longer advertises invented Squad Battles/Rivals/
  Champions options. The endpoint returns an empty options list, leaving the
  mode-specific leaderboard endpoints untouched. The supplied log ended after
  the old options response; in-game Back behavior still requires a client run.
- Draft reward claims now return named `coin` and `pack` awards in the same
  wire format as the working SBC rewards, rather than numeric types that the
  client displayed as `1 Coins`. The 1.0.11 log shows that 750 coins and a
  pack were credited server-side, but its UI showed `1 Coins`; the corrected
  display still needs an in-game check.
- The 1.0.11 log proves the Draft difficulty UI was **not** fixed: Semi-Pro
  was saved server-side while the retail preview showed Ultimate and game
  settings showed Beginner. This remains unresolved; the server-side fields
  added in 1.0.11 are not sufficient evidence of a gameplay fix.
- This remains a complete-source package, including Launch-LocalFUT,
  launcher-settings.json and runtime-source/certs. The x12 Position Modifiers
  Pack, Miz Special Pack and Wez Pack are unchanged.

### 1.0.11 Draft difficulty, coin rewards, Wez and Miz

- The single-player Draft picker keeps its 0–5 menu index, while match launch,
  both match handshakes and the offline-opponent response now use FIFA's 1–6
  gameplay difficulty index. They also include the FUT-specific difficulty
  field. The selected level is no longer sent as the level below it.
- Every Draft finish now grants a nonzero coin amount alongside its earned
  packs. Offline amounts increase with both wins and chosen difficulty;
  previously finished zero-coin runs are repaired at claim time.
- Added the 12-player Wez Pack (86+, at least seven promos) and stronger
  15-player Miz Special Pack (89+, at least twelve promos) to the promo Store.
  Existing promo packs and the x12 Position Modifiers Pack are unchanged.
- Completed Squad Battles wins and losses have regression checks confirming
  a positive, one-time coin credit. Quitting early still awards zero coins.
  The supplied 1.0.10 log contains no completed Squad Battles match, so its
  retail-client reward screen still needs a report from a completed match.

### 1.0.10 SBC reward alignment and Ansu Fati promo exclusion

- Every SBC group now displays one real, named pack that corresponds to the
  reward actually granted on completion. Single-challenge Upgrades show the
  same pack on both the challenge and group reward screens without granting it
  twice. The advanced hybrid groups no longer collapse mixed rewards into a
  misleading “(2)” label.
- FUT Birthday, Shapeshifters and Icon Moments repeatable SBCs now advertise
  their matching guaranteed promo packs. Icons of the Era ends with the new
  three-player Icon Moments Trinity Pack.
- The Icon Moments Showcase reward remains a guaranteed *pack*, not an
  interactive player pick. Native FIFA 20 player-pick presentation has not
  been validated; this build does not falsely label a pack as a choice.
- All Ansu Fati promo resources are excluded from newly generated packs for
  now. His normal silver card and previously owned cards are unaffected.
- The complete-source package retains Launch-LocalFUT, launcher settings,
  certificate/key files, and the unchanged x12 Position Modifiers Pack.

### 1.0.9 SBC rewards, Clear Sold and promo-name fix

- Replaced unsupported Player Pick rewards on the newly added SBCs with real,
  guaranteed themed reward packs. The 90+ Icon Moments SBC grants an Icon
  Moments Star Pack, not a one-of-three choice. This avoids advertising the
  reward type that froze FIFA 20's Challenge Rewards screen.
- Clear Sold now handles the exact `GET /ut/delete/game/fifa20/trade/sold`
  request seen in the retail-client log and persists removal of all sold rows.
- Removed only the `GRADE I`–`GRADE IV` prefixes from the four promo pack
  names, keeping their contents, pricing and odds unchanged.
- Excluded both promo Franck Ribéry resources from newly generated packs.
  Existing owned cards are untouched. The x12 Position Modifiers Pack remains.

### 1.0.8 Record, market, packs, concepts and SBC catalogue (superseded)

- FUT-wide wins, draws and losses now persist across Draft, Squad Battles,
  Rivals, Champions and other completed matches; repeat match reports do not
  increment the club record twice.
- Clearing sold transfer-list cards removes the corresponding auction rows
  permanently, including the native item-ID clear route. Your own live listings
  are hidden from transfer-market search results.
- A regular squad containing a concept card stays under My Squads after a
  restart, while any lineup containing a concept remains non-playable. A
  conservative one-time migration repairs older misclassified mixed squads.
- Broken 93 Ansu Fati and base Franck Ribéry definitions are excluded from
  generated packs. Promo weighting now gives FUT Birthday and Shapeshifters
  meaningful chances, reduces MOTM and Record Breaker frequency, and rotates
  recently pulled special players so Record Breaker Agüero does not recur in
  consecutive top packs.
- The promo Store now has four ascending grades, ending in the 15-player
  88+ Endgame Pantheon Pack with 11 guaranteed promos. The old OtaGoat
  Special Pack is removed, including from migrated Pack Studio settings.
- New advanced and repeatable SBC groups award exclusive promo packs and
  one-of-three/five Player Picks, including a guaranteed 90+ Icon Moments pick.
  SBC submission and validation mechanics remain unchanged.
- This complete-source distribution includes the certificate/key pair,
  Launch-LocalFUT scripts, launcher settings and runtime-source helpers; it
  does not include a compiled launcher. Native in-game pick presentation still
  needs a tester confirmation on the retail FIFA 20 client.

### 1.0.7 Separate Position Modifiers Pack

- Added a permanent Gold Position Modifiers Pack containing exactly 12 usable
  position-change cards selected from the complete 20-card conversion catalog.
- Removed position modifiers from the Gold Consumables Pack. That mixed pack
  continues to contain contracts, fitness, healing, training, chemistry styles,
  and manager-league modifiers.
- Existing consumable ownership, stacking, application, and persistence remain
  unchanged.

### 1.0.4 Native Squad Battles opponent presentation

- Squad Battles opponents are now presented as fictional user-created FUT
  clubs rather than being named after the licensed badge they equipped. Their
  mixed player squads therefore no longer pretend to be Arsenal, Manchester
  City, Borussia Dortmund, or Real Madrid.
- The detail panel receives scalar home/away kit resources, kit team IDs,
  account establishment timestamps, owner names, nations, and locations in
  its native nested `sqbt2data` block. FIFA 20 already consumed the stadium
  from this block; the earlier outer-only aliases were ignored and left the
  generic white shirts, `FUT Founder`, and `INTERNATIONAL` placeholders.
- Refreshed opponent sets receive the same presentation data and top-player
  previews as the initial four opponents.

### 1.0.3 Squad Battles, club packs, player data and promo store

- Squad Battles opponents now use their own real club badge, home and away
  kits, stadium, location, and foundation year instead of the user's active
  club items or blank placeholders. Their displayed rating is calculated from
  the actual starting eleven.
- Base MLS All-Star entries are normalized back to each player's real 2019–20
  MLS club and league before pack generation.
- Club Items Pack reveals use a stable badge/stadium/home/away/ball sequence,
  and portrait prefetch now ignores non-player items to prevent a client-side
  crash seen on one tester's installation.
- The promo store is a four-step low-to-high progression ending in the Ultimate
  Legacy XI Pack. The separate OtaGoat Special Pack contains 25 rare gold
  players rated 83+ and costs exactly 69 coins.
- Ansu Fati carries corrected identity aliases, and a zero-coin Draft loss now
  keeps the native coin reward slot so the following pack count is not shown
  as one coin.

### 0.9.30 Draft-autofill schema and formation compatibility

- Draft autocomplete now returns the dedicated object-shaped native response
  with all 23 generated slots and the selected manager, preventing the client
  from hanging while decoding a Draft-state list.
- Autocomplete selects a random candidate from each generated set instead of
  always taking candidate zero.
- Removed the unsupported `f541` Draft choice that renders as
  `*FUT_FORMATION_FULLNAME_32` with an empty pitch in FIFA 20.

### 0.9.29 Native Draft-autofill transition

- The Final v4 log confirmed that the generated 23-player squad and manager
  were saved, but FIFA froze immediately while decoding the 18 KB autocomplete
  response. The endpoint now returns only its native list-shaped completion
  metadata (under 512 bytes), without embedding the squad document.
- FIFA can then perform its normal Draft-state refresh, which returns the full
  persisted 23-player squad and manager. This keeps both the transition reply
  and the subsequent squad reply in their correct roles.
- The 0.9.28 selected-difficulty match fix remains included.

### 0.9.28 Draft autofill transition and selected match difficulty

- Draft Triangle/autofill now returns a compact completed Draft state containing
  all 23 selected card identities. This gives FIFA the completion transition it
  expects, keeps the user inside Draft, and avoids the roughly 30 KB expanded
  response that previously stalled the interface.
- The selected Single Player Draft difficulty is saved on the run and now sent
  in both match-launch handshakes as `matchDifficulty`, `difficultyId`, and
  `gameDifficulty`. Beginner through Ultimate therefore use the selected value
  instead of silently falling back to Ultimate.
- The normal Draft state endpoint still supplies the complete persistent squad,
  manager, cosmetics, and player data after autofill.

### 0.9.27 Final polish: chemistry, Draft autofill, club variety and TOTY presentation

- Chemistry styles now save FIFA 20's native `cardsubtypeid` (250–273) on the
  player. The owned style is consumed only after the player update succeeds,
  and the changed style remains on the persistent item.
- Draft autofill still builds and saves all 23 player picks plus the manager,
  but its command response is now a small acknowledgement. This avoids feeding
  the native UI a roughly 30 KB state document that made the screen freeze.
- The Gold Club Items Pack now draws from FIFA 20's installed native stadium
  and ball tables instead of repeating Sanderson Park and an invalid ball 191.
  Existing saved ball-191 items are repaired to a valid native ball identity.
- The 85+ x10 pack now keeps purchase ID 7 but uses FIFA 20's valid native
  special-pack presentation shell (asset 4). Rarity 5 still selects the exact
  original FIFA 20 TOTY configuration; the valid shell restores the opening
  sequence assets such as the blue floor runner and side banners/flags.
- The Gold Consumables Pack remains one mixed pack containing contracts,
  fitness, healing, training, chemistry styles, position modifiers, and manager
  league modifiers. No separate chemistry-style pack was added.

### 0.9.26 Correct Draft opponents and stable consumable application

- Draft rounds now build the CPU squad from the selected club: Real Madrid,
  Liverpool, Bayern Munich, then Barcelona. Each opponent uses that club's
  actual FIFA 20 players, badge, home kit, and away kit rather than copying
  the user's active club items.
- Position modifiers and consumables now return the updated native player
  document after application, preventing FIFA from leaving Ultimate Team after
  a successful database change. Chemistry-style identity is corrected in
  0.9.27.
- The Gold Consumables Pack remains one mixed pack containing contracts,
  fitness, healing, training, chemistry styles, position modifiers, and manager
  league modifiers.

### 0.9.24 Draft retry, persistent club items, and position modifiers

Backing out from the controller-placement screen before kickoff is now treated
as a cancelled Draft launch, not a played match. The active run remains ready
for the same round, so returning to the Draft squad and pressing Advance starts
the match normally instead of ending the run with zero wins and a reward.
Draft opponents now rotate by round between Real Madrid, Liverpool, Bayern
Munich, and Barcelona rather than showing Arsenal every match.

All twenty FIFA position-change consumables are supported with their correct
one-step conversions. Applying one updates the player item, consumes the card
only when the conversion is valid, and saves the new position in the persistent
club database. Kits, badges, stadiums, and balls are now owned club items too;
activating one persists the selection across navigation and server restarts.

The Gold Store includes a Gold Position Modifier Pack and a Gold Club Items
Pack. Duplicate visible Gold-pack names are filtered from the Store while
earned My Packs rewards remain untouched.

### 0.9.23 Draft-to-Squad-Battles handoff and authentic rewards

Squad Battles now owns FIFA's second-stage match handshake whenever a Squad
Battles match is active. A completed or abandoned Draft run can no longer
capture that sparse `PUT /match` request and reject it one second after kickoff
with `DRAFT_NOT_READY_FOR_MATCH`. The active Draft record is preserved, while
the Squad Battles match and result continue normally.

All six gold base-player records still carrying the Adidas placeholder club are
corrected at load time: Messi and Rakitić to Barcelona, Marcelo to Real Madrid,
Firmino to Liverpool, and Laporte and Gabriel Jesus to Manchester City. Existing
club items are refreshed automatically. Frenkie de Jong was audited and already
has the correct Barcelona record.

Single-player and online Draft prizes now use FIFA 20-style finish-dependent
reward pools rather than a fixed oversized payout. Squad Battles Bronze 3
through Elite 1 now award the matching FIFA 20 coin and pack combinations.
Hidden Gold, Silver, Prime Mixed Players, and Jumbo Premium Silver reward
definitions were added so every authentic prize opens safely through My Packs.

### 0.9.22 native portrait fallback and standard Store packs

Late-loading square catalog photos are no longer converted into FIFA playerhead
textures. Those files caused the oversized faces and visible white shoulder
rectangles seen across MOTM, Team of the Week, and UCL cards. Only genuine
prepared transparent dynamic canvases are converted; unsuitable images now
fall through to the card's installed EA DDS or FIFA's already-correct native
atlas portrait. A v12 cache ensures no generated square portrait from 0.9.21 is
reused. The verified bundled Icon Moments portraits remain unchanged.

The permanent Store now has a fuller FIFA-style range: regular, Premium, and
Jumbo Premium Bronze and Silver packs, plus Gold, Premium Gold, Jumbo Premium
Gold, Gold Players, Premium Gold Players, Prime Gold Players, Rare Players, and
Jumbo Rare Players packs. Each standard pack uses safe native Bronze/Silver/Gold
art while retaining its own purchase ID. Existing promo odds, Draft behavior,
SBC validation/rewards, and My Packs ownership are unchanged.

### 0.9.21 earned SBC packs and remaining card-data fixes

Earned SBC packs now expose FIFA 20's native `unopened`, `packId`, and
`packAssetId` fields. They are explicitly non-purchasable My Packs rather than
zero-price Store offers, so selecting one goes to the existing pack-open POST
instead of showing an `undefined Coins` purchase dialog. Reward ownership and
one-time consumption remain unchanged.

The portrait converter now identifies prepared full canvases from their actual
dimensions. Square heads found under a custom-image URL are fitted into FIFA's
native 160x160 portrait area instead of being stretched over the whole card.
This applies catalog-wide and uses a new v11 cache, so old oversized cached
images are ignored automatically.

Player-item responses now carry the catalog identity and bio fields needed by
cards absent from the installed FIFA database. This restores Ribéry and Ansu
Fati names without changing their card IDs, pictures, ratings, or attributes.
Marcelo's base Adidas placeholder club is corrected to Real Madrid/LaLiga.

Draft, squad workflows, SBC validation/submission, promo-pack contents, and
the 0.9.20 safe Hub transition are unchanged.

### 0.9.20 SBC-to-Hub crash and normal promo packs

Completing an SBC can remove cards referenced by the active squad. If that
leaves every saved squad incomplete, FIFA 20's native Hub formation builder
previously received zero-ID starter slots and crashed before the Store or My
Packs request was made. The Hub now constructs a complete presentation-only XI
from the remaining owned club players. Submitted cards stay consumed and the
saved squad remains accurately incomplete. An emergency non-persistent catalog
lineup also protects the Hub when fewer than eleven players remain in the club.

The novelty promo packs have been removed. The Promo Store now contains normal
FIFA-style 81+ x2, 82+ x10, 83+ x5, and 85+ x10 Rare Gold Players Packs with
coin prices and controlled promo odds. The 83+ x5 guarantees at least one promo;
the showcase 85+ x10 guarantees five promos and gives every remaining slot a
50% promo chance. Legacy novelty definitions are migrated by exact built-in
name without overwriting genuine Pack Studio creations.

The 0.9.19 Draft layout and SBC reward-ID fixes remain unchanged.

### 0.9.19 Draft summary and SBC finalization fix

Draft now persists FIFA 20's `swapPlayerDefIds` layout whenever a choice is
confirmed. The final Draft squad returned for the Summary and match therefore
uses the latest starting XI, substitutes, and positional swaps instead of the
original pick slots.

Opening an SBC challenge is once again read-only. A plain empty POST always
opens its saved workspace; only FIFA's later native submit request can consume
the squad or mark the challenge complete. All SBC pack prizes now use hidden
My Packs definitions with zero price instead of reusing coin-store articles.
Already-stuck Bronze/Gold rewards from earlier builds are migrated in place the
next time My Packs is opened, so users do not need to reset their database.

Portraits, pack speed, Squad Builder, Transfer Market behavior, and the normal
squad workflows are unchanged.

### 0.9.18 portrait scale and pack-opening speed fix

Ordinary 160x160 player-avatar sources are no longer stretched across FIFA's
220x256 player-head texture. They retain their native scale, are centered on a
transparent native canvas, and therefore no longer cover the card name and
attributes. This is a catalog-wide source-format rule, not a list of exceptions
for the photographed Meunier, Giovinco, Frick, or Eusebio examples. Genuine
custom-card canvases and all corrected Icon Moments portraits keep the 0.9.17
full-canvas treatment.

Pack purchases no longer wait for remote portrait preparation. The supplied
trace showed a 25-special pack blocked for 46.52 seconds and triggered a second
purchase request. Portraits are now prepared concurrently by background daemon
workers while the pack response returns immediately. Preparation is deduplicated
per resource ID and remains identity-keyed, so this does not restore the old
wrong-player portrait behavior. A new v10 portrait cache prevents stretched v9
files from being reused; no manual cache deletion is required.

### 0.9.17 native DDS portrait safety fix

This release replaces the malformed portrait containers shipped in 0.9.16.
Although those files could be decoded as images, their DDS resource header did
not match the native FIFA 20 player-head layout. DirectX therefore rejected the
resource with `E_INVALIDARG`, which could crash pack opening or Player Bio as
soon as a dynamic portrait loaded.

All 93 bundled portraits now use the exact native 220x256 DXT5 header, payload
length, texture flags, and trailer used by an accepted EA CDN player-head file.
Newly downloaded portraits are normalized to that same binary contract before
they can be served. The server also uses a new cache generation and refuses any
non-native cached DDS, so unsafe 0.9.16 cache files are never reused.

The 0.9.16 portrait mapping itself is preserved: Moments and other special
cards still receive the corrected player image and scale. Squad Builder,
Transfer Market buying/bidding/listing, squads, and pack contents are
unchanged.

### 0.9.16 exact dynamic-player portrait mapping

Prime Icon Moments no longer depend on FIFA's revision-atlas fallback. The
release includes verified, native-size 220x256 DXT5 portraits for all 89
Moments cards, preserving each source image's complete prepared canvas instead
of cropping around the face. This fixes oversized Moments artwork while keeping
the correct names, biographies, and attributes introduced in 0.9.15.

Special cards now resolve their dynamic portrait from their exact catalog card
page before the EA CDN/native-atlas fallback is considered. The transparent
source canvas is resized directly and cached under the requested card resource
ID, preventing a failed image request from reusing a previous card's player.
This is a catalog-wide path for all special cards, not a Walker/Verdi-specific
exception. Pillow 12 is now required for the first-time conversion of portraits
that are not bundled; install it with `py -3 -m pip install -r requirements.txt`.

### 0.9.15 Icon Moments identity and player-portrait fix

All 89 Prime Icon Moments cards now use FIFA's native revision-4 player
definition IDs. Their low 24 bits identify the real Icon player instead of a
WeFUT custom-image number, fixing blank names, the ALLSOP fallback, incorrect
biographies, positions, and broken detailed-stat labels. Existing Moments
items are migrated in club items, saved squads, Draft, SBC squads, and market
listings when the server starts.

Pack portraits are now fetched and cached concurrently by exact card resource
ID before the New Items carousel is returned. This removes the serialized
download delay that made FIFA occasionally reuse one card's dynamic portrait
on the next card. The implementation is generic and applies to every player,
not only the captured Verdi/Upamecano example. Squad Builder and Transfer
Market buying/bidding are unchanged.

### 0.9.14 Squad Hub active-formation guard

The supplied 0.9.13 trace proves the crash occurs after the final Hub response,
not while saving a squad. FIFA selects a squad by requesting `/squad/{id}` and
does not send a separate activation request. The server previously returned the
requested populated squad but kept the newest empty squad active; the next Hub
response then fed that empty lineup into CardsDLL's fixed 11-player formation
builder, which dereferenced player zero.

Direct squad loads now synchronize the selected active squad. The Hub also
uses a complete saved squad for its formation presentation whenever the saved
active squad is incomplete, without deleting or overwriting the in-progress
squad. Squad Builder and Transfer Market buying/bidding are unchanged.

### 0.9.13 Native empty-slot teardown fix

Squad responses now preserve FIFA 20's native zero-ID `itemData` placeholder
for every vacant position. The client itself sends this object for all 23
slots, but the server canonicalizer previously removed it. CardsDLL then
retained a null lineup-slot object and crashed while leaving the Squad tab,
especially after creating an empty squad or adding/removing players.

Empty and partially filled squads also return the native five-entry
zero-ID kick-taker array. Placeholders do not count toward squad validity,
captain selection, rating, or chemistry. Squad Builder, Transfer Market
buying and bidding, and existing populated squads are unchanged.

### 0.9.12 Squad save and leave-transition fix

Normal Squad-tab saves now return a complete native squad object instead of
an ID-only acknowledgement. FIFA keeps the save response alive while cards
are added or removed and while the Squad screen transitions back to the Hub;
the partial response left that native object with null fields and caused an
access violation in `CardsDLL_Win64_retail.dll` during teardown.

All 23 player slots, manager, formation, type, validity, and owner identity
are now returned after every successful edit. Squad creation, multi-squad
persistence, embedded-market Buy Now, normal bidding, and Squad Builder are
unchanged.

### 0.9.11 multi-squad and in-squad Buy Now fix

Creating a second or later squad through Squad Selector now allocates a new
persistent squad ID even though FIFA includes the currently selected ID in
its create template. Existing squads remain in the selector, the new squad
becomes active, and stale overwritten references can no longer poison the
Squad tab.

Buy Now from the Transfer Market embedded in the Squad screen now preserves
the auction card's reserved item-instance ID when ownership is delivered.
Both Add Player to Squad and Send Player to Club therefore operate on the
owned item shown on screen instead of an unrelated replacement ID. Normal
bidding and Squad Builder were not changed.

### 0.9.10 native squad-name validation fix

Creating a Regular or Concept Squad now completes FIFA 20's Blaze text
validation step. The native Apply action sends `Util.FilterForProfanity`
(component 9, command 20) before it sends the HTTP squad-create request. The
server now returns FIFA's exact `FilterUserTextResponse` contract, preserving
each submitted name while changing its result from `UNPROCESSED` to `PASSED`.

The complete 0.9.9 squad-list and create responses remain in place. Squad
Builder and all of its working handlers are unchanged.

### 0.9.9 native Squad Selector and creation fix

The Squad Selector now receives FIFA 20's complete squad-list contract:
`activeSquadId` plus full squad objects containing all 23 player slots,
manager data, formation, type, validity, and owner identity. The native client
passes every list row through its full squad factory, so the former compact
six-field summaries caused the immediate selector crash before FIFA could
send the create request.

Regular and Concept Squad creation now return the complete newly created
squad instead of an ID-only acknowledgement. Concept type is inferred from
FIFA's dream-card flags, and the native collection clone route is supported.
Squad Builder and its working handlers are unchanged.

### 0.9.8 squad creation, Squad Builder, and Concept Squad fix

Creating a squad now uses FIFA 20's native squad-list, create, retrieve,
rename, activate, clone, and delete contracts. A name-only update is merged
with the stored squad, so renaming or activating a squad no longer wipes its
lineup. The active squad remains synchronized in persistent storage.

Squad Builder now treats FIFA's `nation=-1`, `league=-1`, and `team=-1`
values as Any instead of impossible filters. It can therefore retrieve the
owned players it needs and save its generated lineup through the same squad
workflow.

Concept Squad searches now use the full player-definition catalog rather
than owned inventory. Concept cards persist as zero-ID dream references and
never duplicate, move, or consume the real club cards. Converting an owned
lineup to a Concept Squad similarly stores concept definitions while leaving
the owned items untouched.

### 0.9.7 My Club search-filter fix

My Club now understands FIFA 20's native broad-position request types:
`playerdefender`, `playermidfielder`, and `playerforward`. Defender searches
return LB/LWB/CB/RB/RWB cards, Midfielder searches return
LM/CDM/CM/CAM/RM cards, and Forward searches return LW/LF/CF/ST/RF/RW cards.
Exact-position filtering remains unchanged.

Gold quality now means normal common or rare Gold cards rated 75 or higher;
special designs are no longer mixed into Gold results. FIFA's native
`rare=SP` filter now returns only special cards. Bronze and Silver quality
filters likewise exclude special designs.

### 0.9.6 native sticker-book schema fix

The 0.9.5 response cleanup was incomplete. Both fresh crash dumps fail at
`CardsDLL_Win64_retail.dll+0x28DB7C`, where FIFA dereferences a required
sticker-book category whose native index is still `-1`. Direct inspection of
the installed FIFA 20 DLL confirms that `/club/stats/year` and
`/club/stats/consumables` share `FutStickerBookStats2ServerResponse` and accept
exactly `category` plus `stat` at the top level.

Both routes now send their required native string enum (`year` or
`consumables`) and statistic rows containing only `contextId`, `contextValue`,
`type`, and `typeValue`. My Club counts are calculated from the persistent
club, and consumable counts are calculated from the owned card stacks. The
working owned-consumable picker, apply endpoint, and Transfer Market are
unchanged.

### 0.9.5 native My Club and Apply Consumable crash fix

The two FIFA crash dumps from Apply Consumable and My Club resolve to the
same invalid-field lookup in `CardsDLL_Win64_retail.dll`. Consumable statistics
now contain only the seven fields accepted by FIFA 20's native consumable
model, with totals calculated from the persistent owned stacks. Unsupported
compatibility rows are no longer sent, so FIFA can continue to the owned-card
picker and the existing persistent apply endpoint.

My Club's `/club/stats/year` response is now a native empty sticker-book
object. The former singular `stat` member was not part of FIFA 20's response
contract and caused the same instant crash; club inventory continues to load
from the separate `/club` request. Staff statistics keep their native
`bonus` collection shape.

### 0.9.4 read-only Compare Price and native consumables

Compare Price now searches only active auctions that already exist in the
persistent Transfer Market. It no longer creates copies of the selected card,
and the normal listing, bidding, buying, and delayed bot-restock simulation is
unchanged. A one-time upgrade migration removes unengaged duplicate batches
created by the old Compare Price implementation while preserving watched or
bid-on cards.

Apply Consumable now opens from a squad player with real owned development
cards instead of an empty native collection. Existing saves receive starter
stacks of Gold Player Contracts, Gold Player Fitness, and Gold Squad Fitness.
The picker receives FIFA's nested stack format, consumable statistics contain
the native context rows, and applying a card updates the player or squad and
decrements the persistent stack.

### 0.9.3 Transfer listing and bid-target status fix

The List on Transfer Market screen now receives FIFA 20's native raw
price-limit array, allowing its start price, Buy Now price, duration, and
confirmation dialog to initialize and submit the existing auction request.

Transfer Targets now supports the native `trade/status/lite` refresh used when
an active bid card is selected. The selected card keeps its current bid and a
live remaining-time value instead of returning an empty response and ejecting
the player from Ultimate Team.

Bot listings now restock on a short 45-second cycle. A Buy Now purchase reduces
the visible Live Transfers total immediately, then a bot replaces the card
after the delay so the market remains active.

### 0.9.2 Transfer List and Compare Price fix

Cards sent to the Transfer List are now returned inside FIFA's native
`auctionInfo` collection as unlisted (`inactive`) owned items. They remain in
the persistent trade pile when the screen is opened, closed, and reopened, and
the client can select them for listing instead of silently discarding them.

Owned player and manager responses now carry their card-aware minimum and
maximum market prices. The listing dialog also supports both FIFA price-limit
routes, including the item-instance form, so start price, Buy Now price, and
duration controls initialize correctly before `List on Transfer Market` is
confirmed.

Compare Price now honors FIFA 20's exact `definitionId` query. Its results are
limited to the same card version and the local bot market supplies a useful
comparison page when that exact card has no current listings. Existing bidding
and Buy Now behavior is unchanged.

### 0.9.1 live Transfer Market protocol fix

The Transfer Market now follows the exact FIFA 20 routes captured from the
game. Bids and Buy Now requests sent to `/trade/{tradeId}/offer` complete
instead of leaving the bidding panel stuck. Buy Now cards are persisted to
Unassigned before FIFA requests `purchased/items`, so Assign Now displays the
actual card and R2 can store it in the Club.

The pre-listing `marketdata/pricelimits` response is implemented, and items
moved to the trade pile appear in the Transfer List even before their auction
is submitted. Listings then preserve the chosen start price, Buy Now price and
duration, remain searchable, and continue through bot bidding, sale, expiry,
relisting, and the 5% sale-tax flow.

The bot market now maintains 200 active listings: normal Gold, Special, Silver,
Bronze and real FIFA 20 manager cards. The `rare=SP`, quality, position, player,
nation, league, club, and price filters are applied server-side. The FUT hub's
Live Transfers number reports the active market total rather than staying at
zero. Existing 0.9.0 databases are upgraded in place and automatically receive
the newly missing market categories.

### 0.9.0 complete local Transfer Market

The Transfer Market now maintains 120 persistent CPU listings made from the
real FIFA 20 player catalog, with a guaranteed mix of normal Gold and Special
cards. Prices are based on card rating, special type, face stats, and a stable
per-card adjustment, then rounded using FIFA's real transfer-price steps.
Search supports exact position, quality, player, nation, league, club, bid,
Buy Now, and pagination filters.

Bidding now reserves coins and awards the card to Unassigned when the auction
ends. Buy Now deducts the correct amount immediately and returns the purchased
card through the normal Assign Now / Continue Searching flow; the existing
item move endpoint then lets FIFA send it to the Club. Watchlist and won-auction
states persist across server restarts.

Owned tradeable players can be listed with the selected start price, Buy Now
price, and duration. Listing a player removes it from saved squads, moves it to
the Transfer List, and makes the user's auction visible in market searches.
Local traders continuously replenish listings, instantly buy attractive Buy
Now offers, and place visible bids on reasonably priced user cards up to their
card-aware budgets. Sold cards remain in the Transfer List until cleared, and
clearing credits the proceeds after FIFA's 5% tax. Expired items can be relisted
or returned to the Club.

### 0.8.4 Draft progression and varied Squad Battles badges

Draft match-end reports are no longer intercepted by the cached result from a
previous Squad Battles match. A native `WIN` now advances the persistent Draft
run through wins 1, 2, 3, and 4, and duplicate reports for the same game ID are
idempotent. After the fourth win, the champion award grants 100,000 coins, one
Rare Mega Pack, and one Rare Players Pack through My Packs.

The four Squad Battles opponents now publish distinct team and badge IDs for
North London, Manchester City, Dortmund, and Madrid instead of all displaying
the Arsenal badge.

### 0.8.3 Squad Battles session handoff + manager test pool

The exact native Squad Battles match request now bypasses any saved Draft run,
including the follow-up `offlinesquad` lookup used during match creation. The
server echoes the selected Squad Battles event ID instead of replacing it with
the local persistence timestamp, preventing a competition mismatch during the
game-session handoff. A regression test covers the captured FIFA 20 payload
(`type=OFFLINE`, event 2957, opponent 0, difficulty 6) while a non-ready Draft
exists.

New and existing clubs also receive an idempotent pool of 12 different gold
managers, including the confirmed-working Unai Emery card. Existing club,
squad, currency, SBC, and mode progress remains intact.

### 0.8.2 native manager asset validation

Manager cards now expose the underlying FIFA database manager ID as `assetId`
while retaining the FUT card ID as `resourceId`/`carddbid`. The 0.8.1 manager
could render in the squad slot but failed FIFA's local pre-match lookup, causing
the misleading "You must assign a Manager" message. Existing 0.8.1 manager
items are migrated automatically on startup, and both native manager contract
fields are retained.

### 0.8.1 Squad Battles manager match fix

Every existing or new club now receives one persistent, real FIFA 20 manager
card when no manager is owned. Squad saves automatically attach that manager,
and the native Squad Battles match payload includes the complete manager item
instead of an unresolved ID. Player searches continue to return players only;
manager searches use their own item type. Existing club items, squads, coins,
SBC progress, and mode progress are preserved.

### 0.8.0 unknown20 native modes port

The working Squad Battles, Division Rivals, and FUT Champions contracts from
the supplied `unknown20` source are now integrated into this newer FIX22 code.
Squad Battles publishes the complete native hub plus four 23-player CPU squads,
serves those squads through both opponent endpoints, and hands the selected
team into `/match`. Rivals and Champions now expose active weekly events,
playable local match handoffs, persistent results, and one-time coin/player
rewards. All current SBC, My Packs, player filtering, and placed-card exclusion
fixes remain intact.

The reference implementation also resolved the earlier ambiguity around
`sqbtOppSquadList`: the bare refresh object is valid when the companion
`squads` collection and nested preview data are fully populated.

### 0.7.0 club picker, Division Rivals, and FUT Champions hubs

The placed-player exclusion from 0.6.5 remains in place. Division Rivals is
enabled and now has a native weekly hub document with an active competition,
Division 5 / 1000 skill rating, ten decoded division definitions, and Weekend
League qualification progress. FUT Champions now answers the native country,
registration, stats-list, and leaderboard requests instead of falling through
to the generic empty-object response. These routes establish the native menu
and progression contracts; local online-match emulation will be developed from
the next captured request sequence. Squad Battles is intentionally unchanged
in this release, as requested.

### 0.6.5 club player-picker exclusion

FIFA sends every player already placed in the current squad through the club
search's `excldef` parameter. Special cards use their full `resourceId` there,
which differs from the base player's `assetId`. The server now checks both IDs,
so a placed base or special card no longer appears as an available search result
for another squad slot. Exact position filters remain server-side.

### 0.6.4 crash-dump-confirmed Squad Battles hub

The `0.6.3` crash dump resolves to `CardsDLL_Win64_retail.dll+0x2A06DB`:
FIFA tries to insert the first opponent through an uninitialised fixed-container
node when `sqbtOppSquadList` is sent as a bare refresh object. The hub keeps the
native fields introduced in `0.6.3`, while property `0x40B` is restored to the
one-element refresh-block array that avoids this crash. This combines the
non-crashing nested shape from `0.6.2` with the complete native hub fields needed
to resolve its loading state.

### 0.6.3 exact FIFA 20 Squad Battles hub schema

The hub now contains only fields handled by FIFA 20's native Squad Battles
decoder. Legacy aliases such as `maxMatches`, `numberOfMatchesPlayed`,
`remainingMatches`, `squadsRemaining`, and `lastMatchUnfinished` were removed.
Their native counterparts (`maxRegularGames`, `gamesPlayed`, match results,
difficulty modifiers, restrictions, and prize tiers) are now populated.
That build also tried `sqbtOppSquadList` as a bare refresh object. The resulting
native crash supplied the missing evidence corrected in `0.6.4` above.

### 0.6.2 native Squad Battles opponent wrapper

The initial hub's `sqbtOppSquadList` now matches the exact nested type decoded
by FIFA 20's CardsDLL: an array containing a refresh block whose fields are
`nextSquadRefreshTimeStamp` and `sqbtOppSquads`. A bare object caused the native
crash; a bare opponent array avoided the crash but was discarded, leaving the
first-time Rewards Every Week screen on its loading overlay indefinitely.

### 0.6.1 playable opponent list and genuine Top 100

This historical attempt corrected the Top 100 and tried a direct opponent
array. FIFA accepted it without crashing but discarded the entries, leaving
the first-time loading overlay unresolved. Version 0.6.2 supersedes that shape.

The Top 100 now contains 100 qualifying CPU scores. The local player appears
only after earning enough Battle Points to displace an entry; a zero-point
account is no longer inserted as rank 100 or used as the initial scroll target.

### 0.6.0 FIFA 20 Squad Battles contract and native SBC submit

Squad Battles now keeps opponent squads out of the hub response and serves
them through FIFA 20's native `/sqbt/v2/user/opponents/refresh` contract. A
native FIFA 20 crash dump identified the old `sqbtOppSquadList` hub property as
the exact client fault. The hub, opponent refresh, opponent squad, history,
match settlement, leaderboard, and weekly reward routes now use separate
response shapes matching the game's own request sequence.

SBC challenge submission now accepts FIFA 20's native `PUT` request, completes
the individual challenge, consumes the submitted cards once, grants its small
challenge reward, and persists the game's `clientdata/squadBuildingSets`
completion marker. Repeating the request is idempotent, so rewards cannot be
claimed twice.

### 0.5.1 native Squad Battles hub crash fix

This historical attempt removed experimental aliases and corrected `rank`, but
still embedded the legacy opponent-list property in the hub response. Version
0.6.0 supersedes it with the FIFA 20-specific split hub/refresh contract.

### 0.5.0 Squad Battles, native SBC counts, and My Packs

Squad Battles now runs as a persistent seven-day local competition. The mode
serves four complete CPU squads per refresh, supports all seven difficulties,
starts playable offline matches, awards difficulty-scaled battle points and
coins, tracks the 30 scoring matches, calculates Bronze through Elite ranks,
and fills the Your Rank and Top 100 screens. Competition progress survives a
server restart. When a played week rolls over, its rank reward can be claimed
once and its pack is added to My Packs.

SBC player totals are no longer sent twice. FIFA 20 now uses its own native
slot count, fixing the permanently-red duplicate such as "Players: Exactly 3"
while server-side submission validation remains enforced for every challenge.
Earned SBC and Squad Battles packs are flagged as owned My Packs and no longer
appear as ordinary zero-coin Store offers.

### 0.4.3 native club picker and unlocked SBC catalog

Player selection now uses FIFA 20's native club-search response (`itemData` and
`itemCount`) and applies position, quality, league, club, nation, rating, loan,
rarity, exclusion, sorting, and pagination filters. Selecting an LW, CM, LB, or
any other squad slot therefore shows only matching owned players. The retired
SBC tutorial gate has also been disabled so every permanent local SBC group is
available immediately.

### 0.4.2 isolated SBC workspace fix

Opening a challenge now returns its own empty SBC squad instead of reusing the
active Ultimate Team squad. FIFA 20's empty challenge `POST` is handled as a
workspace-open request, so pressing Back no longer produces a server
communication error or ejects the player from FUT. Local SBC image requests
also receive a cacheable PNG instead of retrying missing files continuously.

### 0.4.1 native-client crash fix

SBC challenge lists and squads now use FIFA 20's original native wire format
(`elgReq`, native eligibility keys, brick-slot descriptors, 23 squad positions,
and complete invalid-card placeholders). This fixes the instant game exit that
occurred immediately after opening an SBC set.

## Squad Building Challenges

This build adds a persistent, fully local FIFA 20 SBC service. The SBC menu now
contains the permanent FIFA 20 foundation content:

- Basic: Let's Get Started, League and Nation Basics, Let's Keep Going, and
  Let's Move Forward.
- Advanced: Hybrid Leagues, Hybrid Nations, and League and Nation Hybrid.
- Repeatable upgrades: Bronze Upgrade, Silver Upgrade, and Gold Upgrade.

Submitting a valid squad permanently removes those item instances from the
Club, records completion, and grants the challenge reward. Finishing a group
also grants its group reward. Coin rewards update the balance immediately;
pack rewards appear separately under My Packs and can be opened once.

Progress is stored in `runtime\data\localfut20.sqlite3`. Rebuilding/resetting
the offline roster also resets SBC squads, completions, and unopened rewards.

The retired EA player-pick service is not available offline. The four original
Let's Move Forward picks are therefore delivered as one-card, position-matched
Gold rewards capped at 80 OVR (CB, CM, ST, and GK), so every prize remains
claimable from the native Store/My Packs flow.

## What changed from FIX21

FIX21 reached ACCOUNT then stopped before it issued a FUT-auth request.  FIX22 restores the retail FIFA 20 order:

1. `GET /ut/game/fifa20/v2/user/accountinfo` returns only the account-discovery shape (`userAccountInfo.personas[].userClubList[]`).
2. Client calls local `GET /connect/auth?...response_type=code` and receives `{ "code": "LOCAL-FUT20-AUTH-CODE" }`.
3. Client posts that code to local `/ut/auth`; the response now returns the phishing token in both `token` and `X-UT-PHISHING-TOKEN`.
4. The local TLS certificate now covers every hosts-pinned redirector, `accounts.ea.com`, `gateway.ea.com`, and `utas.*` name. The matching local GOS CA modulus is bundled for the existing running-process trust bridge.
5. The launcher treats port 443 as required, so hosts-pinned HTTPS calls cannot fail before an HTTP request reaches the emulator.

## Install
1. Stop old server (Stop-LocalFUT.cmd as Admin).
2. Extract this build over the previous install (or replace the folder).
3. Run Launch-LocalFUT.cmd **as Administrator**.
4. Enter Ultimate Team from main menu.

## Success signals in log
After ACCOUNT you should see this sequence:

- `[FIX22] accountinfo schema=retail-preauth ... expecting /connect/auth then /ut/auth`
- `[FUT-HANDOFF] Nucleus/OAuth host=accounts.ea.com method=GET path=/connect/auth response_type=code`
- `[FUT-HANDOFF] issuing local auth code; expecting POST /ut/auth next ...`
- `[FIX21] /ut/auth ...`
- `[FIX21] HUB reached — marking FUT READY`

If it still dies immediately after accountinfo, send the last 80 lines of `runtime\logs\localfut20.log`, `runtime\server.error.log`, and `runtime\trust-patch.log`.  If no `[FUT-TRANSPORT] scheme=https host=accounts.ea.com` line appears, also send the `FUT TLS` line from `CHECK-LOCAL-FUT.cmd`; that identifies a pre-HTTP TLS failure.
