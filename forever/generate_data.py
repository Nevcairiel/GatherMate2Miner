#!/usr/bin/env python3
import atexit
import os
import re
import json
from dataclasses import dataclass
import typing
import math
import html
import csv
import time

from playwright.sync_api import sync_playwright, Error as PlaywrightError

# Wowhead rejects plain HTTP clients (403 from their firewall), so pages are
# loaded through a locally installed browser instead. Requires
# "pip install playwright"; no browser download is needed.
BROWSER_CHANNEL = os.environ.get('WOWHEAD_BROWSER', 'chrome')  # or 'msedge'

# Fetched pages are kept here so an interrupted or repeated run doesn't hit
# Wowhead again. Delete the directory to fetch fresh data.
CACHE_DIR = 'cache'

# How long to wait (seconds) for a person to solve a CAPTCHA before giving up.
CAPTCHA_TIMEOUT = 1800

_playwright = None
_browser = None
_page = None


def _close_browser():
    if _browser is not None:
        _browser.close()
    if _playwright is not None:
        _playwright.stop()


def _get_page():
    global _playwright, _browser, _page
    if _page is None:
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=False)
        _page = _browser.new_page()
        atexit.register(_close_browser)
    return _page


def _park(page):
    # Leave the current page so its scripts can't start further navigations
    # (some object pages redirect themselves after loading).
    for attempt in range(3):
        try:
            page.goto('about:blank')
            return
        except PlaywrightError:
            time.sleep(1)


def fetch_object_page(object_id) -> str:
    cache_file = os.path.join(CACHE_DIR, f'object-{object_id}.html')
    if os.path.exists(cache_file):
        with open(cache_file, encoding='utf-8') as file:
            return file.read()

    time.sleep(2)  # Rate limiting to avoid being blocked
    page = _get_page()
    url = f'https://www.wowhead.com/forever/object={object_id}'
    text = None
    status = None
    last_error = None
    for attempt in range(5):
        # Read the source as soon as the response arrives; some pages navigate
        # again once their scripts run, after which the source is unavailable.
        try:
            response = page.goto(url, wait_until='commit')
        except PlaywrightError as error:
            # Usually a late navigation by the previous page interrupting this
            # one; retry.
            last_error = error
            _park(page)
            time.sleep(2)
            continue
        status = response.status if response else None
        if status == 202:
            # Interstitial browser check (usually only on the first request). The
            # browser completes it and reloads on its own; wait for the real page,
            # then request it again to get the unmodified source.
            page.wait_for_selector('meta[property="og:title"]', state='attached', timeout=60000)
            continue
        if status == 405:
            # Wowhead is asking for a CAPTCHA. That needs a person: wait for it
            # to be solved in the browser window, then carry on.
            print(f'Wowhead is showing a CAPTCHA (object {object_id}). Please solve it in the browser window...', flush=True)
            page.wait_for_selector('meta[property="og:title"]', state='attached', timeout=CAPTCHA_TIMEOUT * 1000)
            print('CAPTCHA solved, continuing.', flush=True)
            continue
        if status != 200:
            break
        try:
            text = response.text()
            break
        except PlaywrightError as error:
            # The page navigated again before the source could be read; retry.
            last_error = error
            _park(page)
            time.sleep(2)
    _park(page)
    if text is None:
        raise RuntimeError(f'Wowhead returned {status} for object {object_id}' + (f' (last browser error: {last_error})' if last_error else ''))

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_file, 'w', encoding='utf-8') as file:
        file.write(text)
    return text


@dataclass
class WowheadObject:
    name: str
    ids: typing.List[str]
    coordinates: dict
    gathermate_id: str

    def __init__(self, name: str, ids: typing.List[str], gathermate_id: str):
        self.name = name
        self.ids = ids
        self.coordinates = dict()
        self.gathermate_id = gathermate_id

        for object_id in self.ids:
            text = fetch_object_page(object_id)
            title = html.unescape(re.search(r'<meta property="og:title" content="(.*)">', text).group(1))
            data = re.search(r'var g_mapperData = (.*);', text)
            zones = re.findall(r'myMapper.update\({\s+zone: (\d+),\s+level: \d+,\s+}\);\s+WH.setSelectedLink\(this, \'mapper\'\);\s+return false;\s+" onmousedown="return false">([^<]+)</a>', text, re.M)
            zonemap = dict(zones)
            try:
                data_parsed = json.loads(data.group(1))
            except AttributeError:
                print(f"No locations for {object_id} ({self.name})")
                continue
            for zone in data_parsed:
                wow_zone = WOWHEAD_ZONE_MAP.get(zone)
                if wow_zone is None:
                    if zone not in WOWHEAD_ZONE_SUPPRESSION:
                        print(f"Found unlisted zone: {zone} ({zonemap[zone]})")
                    continue
                if wow_zone.name != zonemap[zone]:
                    print(f"Zone name mismatch on {zone}: {wow_zone.name} != {zonemap[zone]}")
                coords = list()
                try:
                    if isinstance(data_parsed[zone], dict):
                        data_dict = data_parsed[zone]
                    else:
                        data_dict = dict(enumerate(data_parsed[zone]))
                    for idx, entry in data_dict.items():
                        for coord in entry["coords"]:
                            coords.append(Coordinate(coord[0], coord[1]))

                    uiMapId = str(entry["uiMapId"])
                    if uiMapId and wow_zone.id != uiMapId:
                        if int(idx) == 0:
                            print(f"Zone mismatch: {wow_zone.id} != {entry['uiMapId']} ({entry['uiMapName']})")
                        else:
                            zoneindex = zone + "-" + str(idx)
                            wow_zone = WOWHEAD_ZONE_MAP.get(zoneindex)
                            if wow_zone is None:
                                print(f"New floor for {zonemap[zone]}: {uiMapId} ({entry['uiMapName']})")
                                wow_zone = Zone(entry["uiMapName"], uiMapId)
                                WOWHEAD_ZONE_MAP[zoneindex] = wow_zone

                    if self.coordinates.get(wow_zone) is None:
                        self.coordinates[wow_zone] = coords
                    else:
                        self.coordinates[wow_zone] += coords
                except KeyError:
                    continue
        if self.name != title:
            print(f"Finished processing {self.name}, but name mismatched! ({title})")
        else:
            print(f"Finished processing {self.name}")


@dataclass(eq=True, unsafe_hash=True)
class Zone:
    name: str
    id: str

    def __init__(self, name: str, id: str):
        self.name = name
        self.id = id
        if UIMAP[id]['name'] != name:
            print(f"UIMap ID <> Name mismatch for {id} ({name} <> {UIMAP[id]['name']})")


@dataclass
class Coordinate:
    x: float
    y: float
    coord: int = 0

    def __repr__(self):
        return str(self.as_gatherer_coord())

    def as_gatherer_coord(self):
        if self.coord == 0:
          self.coord = math.floor((self.x/100)*10000+0.5)*1000000+math.floor((self.y/100)*10000+0.5)*100
        return self.coord


@dataclass
class GathererEntry:
    coordinate: Coordinate
    zone: str
    entry_id: str

    def __repr__(self):
        return f"		[{self.coordinate}] = {self.entry_id},"

    def __lt__(self, other):
        return self.coordinate.as_gatherer_coord() < other.coordinate.as_gatherer_coord()

    def near(self, other, range):
        # Coordinates are percentages of the map (0-100), width/height are the
        # map size in yards, range is in yards.
        rsq = range*range
        x = (self.coordinate.x - other.coordinate.x) / 100 * UIMAP[self.zone]['width']
        y = (self.coordinate.y - other.coordinate.y) / 100 * UIMAP[self.zone]['height']
        distanceSquared = x*x + y*y
        return distanceSquared <= rsq


@dataclass
class GathererZone:
    zone: Zone
    entries: typing.List[GathererEntry]

    def __repr__(self):
        output = f'	[{self.zone.id}] = {{\n'
        for entry in sorted(self.entries):
            output += f'{str(entry)}\n'
        output += '	},\n'
        return output

    def __lt__(self, other):
        return int(self.zone.id) < int(other.zone.id)


@dataclass
class Aggregate:
    type: str
    zones: typing.List[GathererZone]
    count_total: int
    count_skipped: int
    range: int

    def __init__(self, type, objects, range):
        self.type = type
        self.zones = []
        self.count_total = 0
        self.count_skipped = 0
        self.range = range
        for object in objects:
            for zone in object.coordinates:
                for coord in object.coordinates[zone]:
                    self.add(zone, GathererEntry(coord, zone.id, object.gathermate_id))
        print(f'Aggregated {self.count_total} {type} nodes, {self.count_skipped} skipped')

    def __repr__(self):
        output = f"GatherMateData2{self.type}DB = {{\n"
        for zone in sorted(self.zones):
            output += f'{str(zone)}'
        output += '}'
        return output

    def add(self, zone: Zone, entry: GathererEntry):
        self.count_total = self.count_total + 1
        for gatherer_zone in self.zones:
            if gatherer_zone.zone == zone:
                nearby = [x for x in gatherer_zone.entries if x.near(entry, self.range)]
                for node in nearby:
                    if (entry.entry_id == node.entry_id) or (node.entry_id in RARE_SPAWNS and entry.entry_id in RARE_SPAWNS[node.entry_id]):
                        gatherer_zone.entries.remove(node) # replace
                        self.count_skipped = self.count_skipped + 1
                    elif entry.entry_id in RARE_SPAWNS and node.entry_id in RARE_SPAWNS[entry.entry_id]:
                        self.count_skipped = self.count_skipped + 1
                        return # skip
                while entry.coordinate in [x.coordinate for x in gatherer_zone.entries]:
                    entry.coordinate.coord = entry.coordinate.as_gatherer_coord() + 1
                gatherer_zone.entries.append(entry)
                return
        self.zones.append(GathererZone(zone, [entry]))

UIMAP = {}
with open('UiMap.csv', newline='') as uimapcsv:
    reader = csv.reader(uimapcsv)
    next(reader, None)
    for row in reader:
        UIMAP[row[1]] = { 'name': row[0] }

with open('UiMapAssignment.csv', newline='') as assigncsv:
    reader = csv.reader(assigncsv)
    next(reader, None)
    for row in reader:
        uiMapId = row[11]
        orderIndex = int(row[12])
        if not uiMapId in UIMAP:
            print(f'UIMap {uiMapId} not in UIMAP')
            continue
        if orderIndex != 0:
            continue
        if 'width' in UIMAP[uiMapId]:
            print(f'UIMap {uiMapId} has multiple assignments')
            continue
        uiMin0 = float(row[0])
        uiMin1 = float(row[1])
        uiMax0 = float(row[2])
        uiMax1 = float(row[3])
        region0 = float(row[4])
        region1 = float(row[5])
        region2 = float(row[6])
        region3 = float(row[7])
        region4 = float(row[8])
        region5 = float(row[9])
        w = region4 - region1
        h = region3 - region0
        wui = uiMax0 - uiMin0
        hui = uiMax1 - uiMin1
        w2 = w/wui
        h2 = h/hui
        UIMAP[uiMapId]['width'] = w2
        UIMAP[uiMapId]['height'] = h2


RARE_SPAWNS = {
    '204': ['202','203'], # silver
    '205': ['203','206'], # gold
    '208': ['206','214','215'], # truesilver
    '209': ['212','213','207'], # oozed covered silver
    '210': ['212','213','207'], # ooze covered gold
    '211': ['212','213','207'], # oozed covered true silver
    '217': ['206','214','215'], # dark iron
}

# Dungeons and other odd maps
WOWHEAD_ZONE_SUPPRESSION = [
    '6511', '718', '457', '3455', '5339', '1581', '796', '6040', '722', '719', '2100', '2557', '6510', '6514', '1584', '2717', '3479',
    '1583', # Blackrock Spire
]

# Wowhead zone ID (AreaID in UiMapAssignment.csv) -> UiMap
WOWHEAD_ZONE_MAP = {
    '14'    : Zone("Durotar", "1411"),
    '215'   : Zone("Mulgore", "1412"),
    '17'    : Zone("The Barrens", "1413"),
    '36'    : Zone("Alterac Mountains", "1416"),
    '45'    : Zone("Arathi Highlands", "1417"),
    '3'     : Zone("Badlands", "1418"),
    '4'     : Zone("Blasted Lands", "1419"),
    '85'    : Zone("Tirisfal Glades", "1420"),
    '130'   : Zone("Silverpine Forest", "1421"),
    '28'    : Zone("Western Plaguelands", "1422"),
    '139'   : Zone("Eastern Plaguelands", "1423"),
    '267'   : Zone("Hillsbrad Foothills", "1424"),
    '47'    : Zone("The Hinterlands", "1425"),
    '1'     : Zone("Dun Morogh", "1426"),
    '51'    : Zone("Searing Gorge", "1427"),
    '46'    : Zone("Burning Steppes", "1428"),
    '12'    : Zone("Elwynn Forest", "1429"),
    '41'    : Zone("Deadwind Pass", "1430"),
    '10'    : Zone("Duskwood", "1431"),
    '38'    : Zone("Loch Modan", "1432"),
    '44'    : Zone("Redridge Mountains", "1433"),
    '33'    : Zone("Stranglethorn Vale", "1434"),
    '8'     : Zone("Swamp of Sorrows", "1435"),
    '40'    : Zone("Westfall", "1436"),
    '11'    : Zone("Wetlands", "1437"),
    '141'   : Zone("Teldrassil", "1438"),
    '148'   : Zone("Darkshore", "1439"),
    '331'   : Zone("Ashenvale", "1440"),
    '400'   : Zone("Thousand Needles", "1441"),
    '406'   : Zone("Stonetalon Mountains", "1442"),
    '405'   : Zone("Desolace", "1443"),
    '357'   : Zone("Feralas", "1444"),
    '15'    : Zone("Dustwallow Marsh", "1445"),
    '440'   : Zone("Tanaris", "1446"),
    '16'    : Zone("Azshara", "1447"),
    '361'   : Zone("Felwood", "1448"),
    '490'   : Zone("Un'Goro Crater", "1449"),
    '493'   : Zone("Moonglade", "1450"),
    '1377'  : Zone("Silithus", "1451"),
    '618'   : Zone("Winterspring", "1452"),
    '1519'  : Zone("Stormwind City", "1453"),
    '1637'  : Zone("Orgrimmar", "1454"),
    '1537'  : Zone("Ironforge", "1455"),
    '1638'  : Zone("Thunder Bluff", "1456"),
    '1657'  : Zone("Darnassus", "1457"),
    '1497'  : Zone("Undercity", "1458"),
    '616'   : Zone("Mount Hyjal", "2482"),
    '16593' : Zone("Zephras Isle", "2521"),
    '16606' : Zone("Darkspear Islands", "2524"),
    '16591' : Zone("Riverglades", "2548"),
    '16651' : Zone("Shen'dralas", "2652"),
}

HERBS = [
    WowheadObject(name="Peacebloom", ids=['1618', '3724'], gathermate_id='401'),
    WowheadObject(name="Wilted Peacebloom", ids=['656160'], gathermate_id='401'),
    WowheadObject(name="Silverleaf", ids=['1617', '3725'], gathermate_id='402'),
    WowheadObject(name="Stunted Silverleaf", ids=['656161'], gathermate_id='402'),
    WowheadObject(name="Earthroot", ids=['1619', '3726'], gathermate_id='403'),
    WowheadObject(name="Mageroyal", ids=['1620', '3727'], gathermate_id='404'),
    WowheadObject(name="Briarthorn", ids=['1621', '3729'], gathermate_id='405'),
    # WowheadObject(name="Swiftthistle", ids=[], gathermate_id='406'),
    WowheadObject(name="Stranglekelp", ids=['2045'], gathermate_id='407'),
    WowheadObject(name="Bruiseweed", ids=['1622', '3730'], gathermate_id='408'),
    WowheadObject(name="Wild Steelbloom", ids=['1623'], gathermate_id='409'),
    WowheadObject(name="Grave Moss", ids=['1628'], gathermate_id='410'),
    WowheadObject(name="Kingsblood", ids=['1624'], gathermate_id='411'),
    WowheadObject(name="Liferoot", ids=['2041'], gathermate_id='412'),
    WowheadObject(name="Fadeleaf", ids=['2042'], gathermate_id='413'),
    WowheadObject(name="Goldthorn", ids=['2046'], gathermate_id='414'),
    WowheadObject(name="Khadgar's Whisker", ids=['2043'], gathermate_id='415'),
    WowheadObject(name="Wintersbite", ids=['2044'], gathermate_id='416'),
    WowheadObject(name="Firebloom", ids=['2866'], gathermate_id='417'),
    WowheadObject(name="Purple Lotus", ids=['142140'], gathermate_id='418'),
    # WowheadObject(name="Wildvine", ids=[], gathermate_id='419'),
    WowheadObject(name="Arthas' Tears", ids=['142141', '176642'], gathermate_id='420'),
    WowheadObject(name="Sungrass", ids=['142142', '176636'], gathermate_id='421'),
    WowheadObject(name="Blindweed", ids=['142143'], gathermate_id='422'),
    WowheadObject(name="Ghost Mushroom", ids=['142144'], gathermate_id='423'),
    WowheadObject(name="Gromsblood", ids=['142145', '176637'], gathermate_id='424'),
    WowheadObject(name="Golden Sansam", ids=['176583', '176638'], gathermate_id='425'),
    WowheadObject(name="Dreamfoil", ids=['176584', '176639'], gathermate_id='426'),
    WowheadObject(name="Mountain Silversage", ids=['176586', '176640'], gathermate_id='427'),
    WowheadObject(name="Plaguebloom", ids=['176587', '176641'], gathermate_id='428'),
    WowheadObject(name="Icecap", ids=['176588'], gathermate_id='429'),
    # WowheadObject(name="Bloodvine", ids=[], gathermate_id='430'),
    WowheadObject(name="Black Lotus", ids=['176589'], gathermate_id='431'),
    WowheadObject(name="Demonsage", ids=['566786'], gathermate_id='4401'),
    # WowheadObject(name="Marefoil", ids=[], gathermate_id='4402'),
]

ORES = [
    WowheadObject(name="Copper Vein", ids=['1731', '2055', '3763', '103713'], gathermate_id='201'),
    WowheadObject(name="Poor Copper Vein", ids=['562111'], gathermate_id='201'),
    WowheadObject(name="Tin Vein", ids=['1732', '2054', '3764', '103711'], gathermate_id='202'),
    WowheadObject(name="Iron Deposit", ids=['1735'], gathermate_id='203'),
    WowheadObject(name="Silver Vein", ids=['1733', '105569'], gathermate_id='204'),
    WowheadObject(name="Gold Vein", ids=['1734', '150080', '181109'], gathermate_id='205'),
    WowheadObject(name="Mithril Deposit", ids=['2040', '150079', '176645'], gathermate_id='206'),
    WowheadObject(name="Ooze Covered Mithril Deposit", ids=['123310'], gathermate_id='207'),
    WowheadObject(name="Truesilver Deposit", ids=['2047', '181108', '150081'], gathermate_id='208'),
    WowheadObject(name="Ooze Covered Silver Vein", ids=['73940'], gathermate_id='209'),
    WowheadObject(name="Ooze Covered Gold Vein", ids=['73941'], gathermate_id='210'),
    WowheadObject(name="Ooze Covered Truesilver Deposit", ids=['123309'], gathermate_id='211'),
    WowheadObject(name="Ooze Covered Rich Thorium Vein", ids=['177388'], gathermate_id='212'),
    WowheadObject(name="Ooze Covered Thorium Vein", ids=['123848'], gathermate_id='213'),
    WowheadObject(name="Small Thorium Vein", ids=['324', '150082', '176643'], gathermate_id='214'),
    WowheadObject(name="Rich Thorium Vein", ids=['175404'], gathermate_id='215'),
    WowheadObject(name="Dark Iron Deposit", ids=['165658'], gathermate_id='217'),
    WowheadObject(name="Lesser Bloodstone Deposit", ids=['2653'], gathermate_id='218'),
    WowheadObject(name="Incendicite Mineral Vein", ids=['1610', '1667'], gathermate_id='219'),
    WowheadObject(name="Indurium Mineral Vein", ids=['19903'], gathermate_id='220'),
    WowheadObject(name="Heavy Thorium Vein", ids=['566779'], gathermate_id='4201'),
]

if __name__ == '__main__':
    # Herbs
    with open("../../GatherMate2_Data/forever/HerbalismData.lua", "w") as file:
        print(Aggregate("Herb", HERBS, 15), file=file)

    # Ores
    with open("../../GatherMate2_Data/forever/MiningData.lua", "w") as file:
        print(Aggregate("Mine", ORES, 15), file=file)
