# Nova Scotia Water Well Viewer

An interactive web map of about 125,000 water wells in Nova Scotia, built from the province's public
[well log database](https://novascotia.ca/nse/groundwater/). Wells are coloured by type (drilled or
dug) and can be filtered by the year they were completed. Clicking a well shows its details.

**Live app: [nswellsvibecoded.vercel.app](https://nswellsvibecoded.vercel.app/)** (hosted on Vercel)

The app is a single static HTML page with no backend. It reads two data files straight from the
web server:

- **`wells.pmtiles`:** vector map tiles containing every well's location, ID, type and year.
- **`well_details.parquet`:** the full attributes. The page fetches only the small part it needs
  when a well is clicked.

The repo was built in a conversation with [Claude Code](https://claude.com/claude-code). The
[How this repo was built](#how-this-repo-was-built) section below records the prompts and the
decisions made along the way.

---

## Quick start

The map's data files are committed, so the app runs straight after cloning:

```bash
# 1. Serve the folder with a server that supports HTTP Range requests
npx http-server          # needs Node 18+; with nvm: `nvm use 23` first

# 2. Open the printed URL, e.g. http://127.0.0.1:8080
```

To rebuild the data from the province's database, for example after it's updated, run
`python build_data.py --refresh`. See [The data pipeline](#the-data-pipeline-build_datapy) for
its requirements.

The page **must be served over HTTP**. Opening `index.html` as a file (`file://`) won't work.
The server must also support **Range requests**, which `python -m http.server` doesn't.
`npx http-server` and most static hosts do.

---

## Repository contents

| File | What it is |
|---|---|
| `index.html` | The map app: MapLibre GL, PMTiles and hyparquet, all loaded from CDNs. |
| `build_data.py` | The data pipeline: Access database → GeoPackage → Parquet and PMTiles. |
| `wells.pmtiles` | *Generated, committed.* Map tiles, zoom 0–13, layer `wells`. |
| `well_details.parquet` | *Generated, committed.* All attributes, sorted by `well_id`. |
| `wells.gpkg` | *Generated, not committed.* One point per well, NAD83 / UTM zone 20N (EPSG:26920). |
| `NS_Well_Logs_2020.zip`, `NS Well Logs (2020) - Public Version.mde` | *Downloaded* by `build_data.py`. |
| `jdbc/` | *Downloaded* by `build_data.py`: the UCanAccess JDBC driver jars. |

> The code, this README and the app's two data files are committed. `.gitignore` excludes
> `wells.gpkg` and the *Downloaded* files: `build_data.py` recreates them, and the `.mde` (about
> 140 MB) is over GitHub's 100 MB file limit anyway.

---

## The data pipeline: `build_data.py`

### Requirements

| Tool | Why | Install (macOS) |
|---|---|---|
| Python 3 with `duckdb`, `JayDeBeApi`, `JPype1` | Runs the pipeline and talks to the JDBC driver | `pip install duckdb JayDeBeApi JPype1` |
| Java 11+ | Runs the UCanAccess JDBC driver | [Eclipse Temurin](https://adoptium.net/) JDK |
| GDAL (`ogr2ogr`) | Writes the GeoPackage and reprojects points | `brew install gdal` |
| tippecanoe | Builds the PMTiles | `brew install tippecanoe` |

On Windows, run the pipeline under WSL, because tippecanoe doesn't run natively on Windows.

### Running it

```bash
python build_data.py                    # download the zip (first run only), extract, build
python build_data.py --refresh          # download the zip again, e.g. after the province updates it
python build_data.py path/to/logs.mde   # build from an Access file you already have
```

A full run takes about 1–2 minutes and ends with:

```
wells.gpkg: 125,517 wells
well_details.parquet: 125,517 rows
Done: wells.gpkg + wells.pmtiles + well_details.parquet
```

### What it does

**0. Fetch the source data.** The script downloads
`https://novascotia.ca/nse/groundwater/docs/NS_Well_Logs_2020.zip` and extracts the Access database
from it. It also downloads the UCanAccess, Jackcess and HSQLDB jars from Maven Central into `jdbc/`.
Each download is saved under a temporary name and renamed only when complete, so an interrupted
download is never mistaken for a finished file. Only the `.mde` is taken from the zip, and it is
written by its bare file name.

**1. Access → `wells.gpkg`.** The script reads `tblWellLogs` over JDBC and joins three lookup tables
to turn codes into text:

| Join | Gives |
|---|---|
| `tblWellLogs.DugOrDrilled` → `tblDugOrDrilled.LookupCode` | `well_type` (`DRILLED`, `DUG`, `UNKNOWN`) |
| `tblWellLogs.CountyL` → `tblCounty.LookupCode` | `county` (e.g. `HALIFAX`) |
| `tblWellLogs.UTMAccuracy` → `tblUTMAccuracy.LookupCode` | `coordinate_accuracy` (50, 800, 1500 or 8000 m) |

Columns are renamed as follows:

| Access column | GeoPackage column | Type |
|---|---|---|
| `WellNumber` | `well_id` | String |
| `DugOrDrilled` (via lookup) | `well_type` | String |
| first 4 characters of `DateWellCompleted` | `date_well_completed` | String (a year) |
| `CivicAddress` | `civic_address` | String |
| `LotNumber` | `lot_number` | String |
| `Subdivision` | `subdivision` | String |
| `PropertPID` | `property_pid` | String |
| `CountyL` (via lookup) | `county` | String |
| `PostalCode` | `postal_code` | String |
| `DepthToBedrock` | `depth_to_bedrock` | Real (Float32) |
| `TotalOrFinishedDepth` | `total_finished_depth` | Real (Float32) |
| `Easting` | `easting` | Integer |
| `Northing` | `northing` | Integer |
| `UTMAccuracy` (via lookup) | `coordinate_accuracy` | Integer |

Points are built from `easting`/`northing` in NAD83 / UTM zone 20N. The 343 records without
coordinates are dropped, which leaves 125,517 wells. The layer is a MultiPoint layer named `wells`,
with feature id `id` and geometry column `geom`, the same layout as the earlier GeoPackage.

**2. `wells.gpkg` → `well_details.parquet`.** DuckDB writes every attribute to Parquet with these
settings:

- **Sorted by `well_id`, in 2,000-row groups.** Each group then covers a narrow range of IDs, so
  a click downloads one group of about 50 KB instead of the whole 3 MB file.
- **Snappy compression.** The hyparquet build the page uses can't decode ZSTD.
- **Year stored as an integer.**

**3. `wells.gpkg` → `wells.pmtiles`.** ogr2ogr reprojects the points to WGS84 as single 2D points in
a temporary FlatGeobuf. tippecanoe then tiles them with `-Z0 -z13 -r1 --drop-densest-as-needed`.
Zoom 13 keeps every well; lower zooms are thinned to keep tiles small. The tiles carry only
`well_id`, `well_type` and `date_well_completed`, with the year forced to an integer (`-T`) so the
map can filter it by range.

---

## The app: `index.html`

- **Basemap:** OpenFreeMap Positron (no API key), centred on Nova Scotia, with zoom and scale
  controls.
- **Well points:** coloured by type: blue for drilled, orange for dug, grey for anything else. A
  legend sits in the bottom-left corner.
- **Year filter (top left):** a two-handle "Year completed" slider.
  - **Range from the data:** it reads the 1920–2020 range from the tile file's metadata, so it adjusts
    itself when the data is rebuilt.
  - **Uneven scale:** most wells are recent, so the scale is curved to give recent years more room.
    The newest half of the track covers 1995–2020, and the sparse 1920–1960 years are squeezed into
    about a fifth. Tick labels (1920, 1960, 1980, 2000, 2010, 2020) show the spacing.
  - **Handles:** they can't cross. Arrow keys step one year, Page Up/Down ten years, and Home/End
    jump to the ends.
  - **Show all:** resets the range. At the full range no filter is applied.
- **Details popup:** clicking a well shows its ID, a type badge, year completed, civic address, lot,
  subdivision, PID, county, postal code, depth to bedrock, total depth, UTM coordinates and
  coordinate accuracy. The page reads only the Parquet file's footer once, then only the row group
  holding the clicked ID.

Settings such as file URLs, colours and popup labels are in the `CONFIG` block at the top of the
page's script.

**Known limitation:** below zoom 8 the tiles hold a thinned sample of wells, so a filtered view of the
whole province shows a sample of the matching wells. From zoom 8 up, every matching well is shown.

---

## How this repo was built

The repo was built with Claude Code in one session. The first versions of `index.html` and
`build_data.py` came from an earlier Claude conversation; this session started by checking them.
The prompts below are quoted as they were given.

### 1. Checking the existing app

> *"I have this Claude created index.html file which references two files: wells.pmtiles and
> well_details.parquet, can you confirm the data will load properly."*

Claude tested both files with the same libraries the page uses. The page loaded, but the data had
two problems:

- **Missing wells:** the tiles stopped at zoom 5, where tippecanoe had dropped about 57% of the
  wells, so they never appeared on the map.
- **Slow clicks:** the Parquet file wasn't sorted by `well_id`, so each click downloaded about
  2.6 MB.

### 2. Fixing the build script

> *"can you update the build_data.py based on the wells.gpkg in the folder"*

The fixes:

- **Empty export:** the GeoPackage declared its geometry as XYZM MultiPoint but stored 2D points.
  The FlatGeobuf writer rejected every row, so the script now forces plain 2D points.
- **Unreadable Parquet:** the file was written with ZSTD, which the page's hyparquet build can't
  decode, so every popup would have failed. The script now uses Snappy.
- **Missing wells:** the tiles now go to zoom 13 and keep every well there.

> *"I'm getting an error: subprocess.CalledProcessError: Command '['ogr2ogr', ... ]' returned
> non-zero exit status 1."*

The FlatGeobuf driver can't `-overwrite`, so the script now deletes any leftover `wells.fgb`
first.

> *"I ran npx http-server and I get an error: Error: Cannot find module 'node:path'"*

The default Node was v12, which is too old for `http-server`. The fix is `nvm use 23`.

### 3. Adding the year filter

> *"The wells.gpkg has been updated with valid date_well_completed values. Can you regenerate the
> parquet and pmtiles data and add a filter by year to the map app, this will require the
> date_well_completed in the pmtiles data."*

- **Year as an integer:** the dates were 4-digit years stored as text, so the year is stored as an
  integer in both files to allow range filtering.
- **Text-to-number fix:** OGR's SQL step turned the year back into text, so tippecanoe's `-T` option
  forces it to an integer.
- **Slider range from the data:** the slider reads its range from the tile metadata rather than
  hard-coded years.

> *"Can you update the date slider scale to give more granularity for the more recent years."*

Only about 1.5% of wells predate 1960, yet that period took 40% of a linear slider. The slider now
uses a curved scale (`year = max − span·(1−p)²`). Tick labels make the spacing visible, and
keyboard handling keeps arrow keys stepping one whole year.

### 4. Building from the original Access database

> *"The wells.gpkg is not the original data ... I'd like to update the build_data.py to build from
> the original data, a MSAccess file ... You'll need to use a JDBC connector to connect to it."*

The prompt also gave the three lookup joins, the column renames, the year rule and the coordinate
system, and asked for output that looks like the old GeoPackage.

- **Driver:** UCanAccess, a pure-Java Access JDBC driver, called from Python with JayDeBeApi and
  JPype.
- **Java:** Homebrew couldn't install OpenJDK on this Intel Mac running macOS 26, a Tier 3 platform
  for Homebrew. An Eclipse Temurin JDK was installed per-user instead.
- **JVM path:** JPype returned the Java home folder instead of the JVM library, so the script finds
  the library itself.
- **Column names:** the JDBC driver reports the original Access column names rather than the `AS`
  aliases, so result columns are matched by position, with a count check.
- **Writing the GeoPackage:** rows pass through a CSV plus a `.csvt` type file into ogr2ogr, which
  reproduces the old GeoPackage's field types and widths.
- **Checked against the old file:** the same 125,517 wells, identical values in 11 of 13 attribute
  columns, and 125,516 of the points byte-for-byte identical. The intended differences are listed
  below.

| Difference from the earlier GeoPackage (`wells_old.gpkg`, since removed) | Reason |
|---|---|
| Geometry declared as 2D MultiPoint, not XYZM | The points were always 2D; the XYZM declaration broke the tile export. |
| 18 empty civic addresses are NULL instead of `''` | The CSV step can't tell the two apart. |
| Two values (`well_id` `1604⏎75`, one `property_pid`) have `\n` instead of `\r\n` | Line breaks typed into the Access data; the CSV step normalises them. |
| Feature ids follow `well_id` order | The old tool numbered rows in the Access file's physical storage order. |

### 5. Downloading the source automatically

> *"So the MSAccess data base ... can be found online at
> 'https://novascotia.ca/nse/groundwater/docs/NS_Well_Logs_2020.zip', can you update the
> build_data.py to download the zip file and extract it, then run the remainder of the script."*

The script now downloads and extracts the zip on its first run and reuses them after that;
`--refresh` downloads again. The downloaded `.mde` was confirmed byte-identical to the local copy.
A run in an empty folder produced the same output as before.

### How changes were checked

Each step was tested rather than assumed:

- **Data files:** read with the same libraries the page uses (pmtiles and hyparquet in Node), with
  the page's lookup logic replayed against them.
- **App:** loaded in headless Chrome to drive the slider, check the filter and take screenshots.
- **GeoPackage:** compared row by row in SQLite against the earlier GeoPackage, which was then
  removed from the repo.

---

## Data source and licence

Well data: Nova Scotia Department of Environment and Climate Change,
*NS Well Logs (2020) – Public Version*. Check the province's terms of use before redistributing
the data. Basemap © OpenFreeMap and © OpenStreetMap contributors.
