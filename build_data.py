"""
Build the water well data from the original MS Access database:

  wells.gpkg           - one point per well with the public attributes
                         (NAD83 / UTM zone 20N, same layout as the earlier GeoPackage)
  wells.pmtiles        - map points (well_id, well_type, year completed), for drawing
                         and filtering the map
  well_details.parquet - every attribute, sorted by well_id, fetched on click

Requirements:
  pip install duckdb JayDeBeApi JPype1
  A Java runtime, 11 or newer (macOS: an Adoptium Temurin JDK)
  ogr2ogr (GDAL) and tippecanoe on your PATH
  (macOS: brew install gdal tippecanoe; on Windows use WSL)
  The UCanAccess JDBC driver jars are downloaded into ./jdbc on first run.

Usage:
  python build_data.py                    # download the province's zip (first run only),
                                          # extract the Access file, and build
  python build_data.py --refresh          # download the zip again, e.g. after an update
  python build_data.py path/to/logs.mde   # or build from an Access file you already have
"""
import csv
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
import duckdb

# Nova Scotia well logs, published by the province as a zipped Access database
SOURCE_URL = "https://novascotia.ca/nse/groundwater/docs/NS_Well_Logs_2020.zip"
SOURCE_ZIP = os.path.basename(SOURCE_URL)
SOURCE_DB = "NS Well Logs (2020) - Public Version.mde"

args = sys.argv[1:]
REFRESH = "--refresh" in args
args = [a for a in args if a != "--refresh"]
ACCESS_DB = args[0] if args else None  # None: download and extract SOURCE_DB
GPKG = "wells.gpkg"
LAYER = "wells"
GEOM = "geom"

# UCanAccess (pure-Java Access driver) and its dependencies, from Maven Central
JDBC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jdbc")
JDBC_JARS = [
    "io/github/spannm/ucanaccess/5.2.0/ucanaccess-5.2.0.jar",
    "io/github/spannm/jackcess/5.2.0/jackcess-5.2.0.jar",
    "org/hsqldb/hsqldb/2.7.4/hsqldb-2.7.4.jar",
]

# One row per well with coordinates; lookup codes replaced by their text and
# columns renamed to the GeoPackage's names. Ordered by well_id so the
# GeoPackage's feature ids follow the well numbers.
WELLS_SQL = """
    SELECT w.WellNumber           AS well_id,
           d.DugOrDrilled         AS well_type,
           w.DateWellCompleted    AS date_well_completed,
           w.CivicAddress         AS civic_address,
           w.LotNumber            AS lot_number,
           w.Subdivision          AS subdivision,
           w.PropertPID           AS property_pid,
           c.County               AS county,
           w.PostalCode           AS postal_code,
           w.DepthToBedrock       AS depth_to_bedrock,
           w.TotalOrFinishedDepth AS total_finished_depth,
           w.Easting              AS easting,
           w.Northing             AS northing,
           u.UTMAccuracy          AS coordinate_accuracy
    FROM ((tblWellLogs w
         LEFT JOIN tblDugOrDrilled d ON w.DugOrDrilled = d.LookupCode)
         LEFT JOIN tblCounty c       ON w.CountyL = c.LookupCode)
         LEFT JOIN tblUTMAccuracy u  ON w.UTMAccuracy = u.LookupCode
    WHERE w.Easting IS NOT NULL AND w.Northing IS NOT NULL
    ORDER BY w.WellNumber
"""

# GeoPackage field types, in column order (matching the earlier GeoPackage)
GPKG_FIELDS = {
    "well_id":              "String(255)",
    "well_type":            "String(8)",
    "date_well_completed":  "String(200)",
    "civic_address":        "String(50)",
    "lot_number":           "String(10)",
    "subdivision":          "String(50)",
    "property_pid":         "String(10)",
    "county":               "String(50)",
    "postal_code":          "String(10)",
    "depth_to_bedrock":     "Real(Float32)",
    "total_finished_depth": "Real(Float32)",
    "easting":              "Integer",
    "northing":             "Integer",
    "coordinate_accuracy":  "Integer",
}

# Columns written to the details file (everything except geometry)
DETAIL_COLUMNS = list(GPKG_FIELDS)

# date_well_completed holds a 4-digit year as text; store it as an integer so
# the map can filter it by range and the popup shows a plain year.
YEAR_EXPR = "CAST(date_well_completed AS INTEGER) AS date_well_completed"

# Columns kept in the map tiles (keep this small: it goes into every tile)
TILE_COLUMNS = ["well_id", "well_type", YEAR_EXPR]

# Smaller row groups = less data downloaded per click.
# 2,000 rows gives ~63 groups for 125k wells.
ROW_GROUP_SIZE = 2000

# Highest zoom level stored in the tiles. Every well is kept at this zoom;
# lower zooms are thinned to keep tiles small. The map overzooms past it.
MAX_ZOOM = 13


def run(cmd):
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True)


def download(url, path):
    """Download to a temporary name first, so an interrupted download
    never leaves a partial file that looks complete."""
    print(f"Downloading {url}")
    urllib.request.urlretrieve(url, path + ".part")
    os.replace(path + ".part", path)


def fetch_source_db(refresh=False):
    """Download the province's zip (unless already here) and extract the
    Access database from it; return the database's path."""
    if refresh or not os.path.exists(SOURCE_ZIP):
        download(SOURCE_URL, SOURCE_ZIP)
    if refresh or not os.path.exists(SOURCE_DB) \
            or os.path.getmtime(SOURCE_DB) < os.path.getmtime(SOURCE_ZIP):
        with zipfile.ZipFile(SOURCE_ZIP) as z:
            # Extract only the .mde, by its bare file name, so paths stored in
            # the zip can't write anywhere outside this folder
            members = [m for m in z.namelist() if os.path.basename(m) == SOURCE_DB]
            if not members:
                sys.exit(f"{SOURCE_DB} not found in {SOURCE_ZIP}: {z.namelist()}")
            print(f"Extracting {SOURCE_DB}")
            with z.open(members[0]) as src, open(SOURCE_DB + ".part", "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(SOURCE_DB + ".part", SOURCE_DB)
    return SOURCE_DB


def jdbc_jars():
    """Download the UCanAccess jars on first use; return their paths."""
    os.makedirs(JDBC_DIR, exist_ok=True)
    paths = []
    for jar in JDBC_JARS:
        path = os.path.join(JDBC_DIR, os.path.basename(jar))
        if not os.path.exists(path):
            download(f"https://repo1.maven.org/maven2/{jar}", path)
        paths.append(path)
    return paths


def jvm_path(jpype):
    """Path to the JVM library. JPype sometimes returns the Java home folder
    instead (seen on macOS), so look for the library inside it."""
    path = os.fsdecode(jpype.getDefaultJVMPath())
    if os.path.isdir(path):
        for lib in ("lib/server/libjvm.dylib", "lib/server/libjvm.so", "bin/server/jvm.dll"):
            if os.path.exists(os.path.join(path, lib)):
                return os.path.join(path, lib)
    return path


def read_access_wells(db_path):
    """Run WELLS_SQL against the Access file; return its rows."""
    import jpype
    import jaydebeapi

    jars = jdbc_jars()
    jpype.startJVM(jvm_path(jpype), classpath=jars, convertStrings=True)
    # UCanAccess warns about saved Access queries that reference forms; they
    # aren't used here. Keep a reference so Java doesn't drop the setting.
    global _ucanaccess_log
    _ucanaccess_log = jpype.JClass("java.util.logging.Logger").getLogger("net.ucanaccess")
    _ucanaccess_log.setLevel(jpype.JClass("java.util.logging.Level").SEVERE)

    url = f"jdbc:ucanaccess://{os.path.abspath(db_path)};memory=false"
    con = jaydebeapi.connect("net.ucanaccess.jdbc.UcanaccessDriver", url, [], jars)
    try:
        cur = con.cursor()
        cur.execute(WELLS_SQL)
        # The driver reports the Access column names, not the AS aliases, so
        # rows are matched to DETAIL_COLUMNS by position: check the count.
        assert len(cur.description) == len(DETAIL_COLUMNS), cur.description
        rows = cur.fetchall()
    finally:
        con.close()
    return rows


# Fail fast if a tool is missing, before doing any work
missing = [t for t in ("ogr2ogr", "tippecanoe") if shutil.which(t) is None]
if missing:
    sys.exit(f"Missing on PATH: {', '.join(missing)} (macOS: brew install {' '.join(missing)})")
if ACCESS_DB is None:
    ACCESS_DB = fetch_source_db(REFRESH)
elif not os.path.exists(ACCESS_DB):
    sys.exit(f"Access database not found: {ACCESS_DB}")

# 1. Access -> GeoPackage. Rows go through a CSV (plus a .csvt giving each
# column's GeoPackage type) so ogr2ogr builds the points and the layer.
print(f"Reading {ACCESS_DB}")
rows = read_access_wells(ACCESS_DB)
columns = DETAIL_COLUMNS
year = columns.index("date_well_completed")
east, north = columns.index("easting"), columns.index("northing")
with tempfile.TemporaryDirectory() as tmp:
    csv_path = os.path.join(tmp, "wells.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        out = csv.writer(f)
        # x/y are copies of easting/northing used only to build the points
        out.writerow(columns + ["x", "y"])
        for row in rows:
            row = list(row)
            # Timestamps arrive as "YYYY-MM-DD hh:mm:ss"; keep the year
            if row[year] is not None:
                row[year] = str(row[year])[:4]
            out.writerow(row + [row[east], row[north]])
    with open(os.path.join(tmp, "wells.csvt"), "w") as f:
        f.write(",".join(f'"{t}"' for t in [*GPKG_FIELDS.values(), "Real", "Real"]) + "\n")

    if os.path.exists(GPKG):
        os.remove(GPKG)
    # EMPTY_STRING_AS_NULL: missing values become NULL rather than ""
    run(["ogr2ogr", "-f", "GPKG", GPKG, csv_path,
         "-oo", "X_POSSIBLE_NAMES=x", "-oo", "Y_POSSIBLE_NAMES=y",
         "-oo", "KEEP_GEOM_COLUMNS=NO", "-oo", "EMPTY_STRING_AS_NULL=YES",
         "-a_srs", "EPSG:26920", "-nlt", "MULTIPOINT", "-nln", LAYER,
         "-lco", "FID=id", "-lco", f"GEOMETRY_NAME={GEOM}"])
print(f"{GPKG}: {len(rows):,} wells")

# 2. Details -> Parquet, sorted by well_id so a lookup touches one row group.
# SNAPPY, not ZSTD: the hyparquet build the page loads can't decode ZSTD.
con = duckdb.connect()
con.execute("INSTALL spatial; LOAD spatial;")
con.execute("SET preserve_insertion_order = true;")
cols = ", ".join(YEAR_EXPR if c == "date_well_completed" else c for c in DETAIL_COLUMNS)
con.execute(f"""
    COPY (
        SELECT {cols}
        FROM ST_Read('{GPKG}', layer = '{LAYER}')
        ORDER BY well_id
    ) TO 'well_details.parquet'
    (FORMAT parquet, ROW_GROUP_SIZE {ROW_GROUP_SIZE}, COMPRESSION snappy);
""")
n = con.execute("SELECT count(*) FROM 'well_details.parquet'").fetchone()[0]
print(f"well_details.parquet: {n:,} rows")

# 3. Map points -> FlatGeobuf in WGS84 -> PMTiles, as single 2D points.
# The FlatGeobuf driver can't -overwrite, so remove any leftover file first.
if os.path.exists("wells.fgb"):
    os.remove("wells.fgb")
tile_sql = f'SELECT {", ".join(TILE_COLUMNS)}, "{GEOM}" FROM "{LAYER}"'
run(["ogr2ogr", "-f", "FlatGeobuf", "-t_srs", "EPSG:4326",
     "-dim", "XY", "-nlt", "POINT", "-explodecollections",
     "-dialect", "SQLite", "-sql", tile_sql, "-nln", "wells", "wells.fgb", GPKG])

# OGR's SQLite dialect drops the CAST's type, so the year reaches the .fgb as
# text; -T makes tippecanoe store it as a number for the map's range filter.
run(["tippecanoe", "-o", "wells.pmtiles", "--force", "-l", "wells",
     "-Z0", f"-z{MAX_ZOOM}", "-r1", "--drop-densest-as-needed",
     "-T", "date_well_completed:int", "wells.fgb"])
os.remove("wells.fgb")

print(f"Done: {GPKG} + wells.pmtiles + well_details.parquet")
