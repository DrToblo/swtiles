```markdown
# SWTILES Project Status

## Project Overview

**Goal**: Create a single-file tile archive format for serving Swedish orthophoto and terrain data via HTTP range requests from Cloudflare R2 (or similar cloud storage).

**Data Source**: Lantmäteriet (Swedish Land Survey) open data
- Orthophoto: 1m resolution WebP tiles, ~1.9M tiles, ~31 GB
- Terrain (Terrain-RGB encoded): 1m resolution PNG tiles, ~1.9M tiles, ~153 GB

**Target Use Case**: Browser-based map viewer that fetches individual tiles via HTTP range requests without needing a tile server.

---

## SWTILES Format v2.0

### Design Goals
- Single-file archive containing multiple resolution levels
- O(1) tile lookup by coordinate or grid position
- HTTP range-request friendly (2 requests per tile: index + data)
- Self-describing (all parameters stored in file)
- Arbitrary CRS support (SWEREF 99 TM / EPSG:3006)

### File Structure

```
┌─────────────────────────────────────────────────────────────────┐
│ FILE HEADER (256 bytes)                                         │
├─────────────────────────────────────────────────────────────────┤
│ LEVEL TABLE (64 bytes × num_levels)                            │
├─────────────────────────────────────────────────────────────────┤
│ LEVEL SECTIONS (repeated for each level)                        │
│   ├── Level Index (8 bytes × grid_cols × grid_rows)            │
│   └── Level Tile Data (concatenated image blobs)               │
└─────────────────────────────────────────────────────────────────┘
```

### Header (256 bytes)

| Offset | Size | Type    | Field              | Description                          |
|--------|------|---------|--------------------|--------------------------------------|
| 0      | 8    | char[8] | magic              | "SWTILES\0"                          |
| 8      | 2    | uint16  | version            | Format version (currently 2)         |
| 10     | 1    | uint8   | data_type          | 1=raster, 2=terrain, 3=other        |
| 11     | 1    | uint8   | image_format       | 1=webp, 2=png, 3=jpg, 4=avif        |
| 12     | 4    | uint32  | crs_epsg           | EPSG code (3006 for SWEREF 99 TM)   |
| 16     | 8    | float64 | bounds_min_e       | Minimum easting                      |
| 24     | 8    | float64 | bounds_min_n       | Minimum northing                     |
| 32     | 8    | float64 | bounds_max_e       | Maximum easting                      |
| 40     | 8    | float64 | bounds_max_n       | Maximum northing                     |
| 48     | 2    | uint16  | tile_size_px       | Tile dimension (500)                 |
| 50     | 1    | uint8   | num_levels         | Number of resolution levels          |
| 51     | 1    | uint8   | reserved           | Must be 0                            |
| 52     | 8    | uint64  | level_table_offset | Byte offset to level table           |
| 60     | 196  | bytes   | reserved           | Must be 0                            |

### Level Table Entry (64 bytes)

| Offset | Size | Type    | Field           | Description                           |
|--------|------|---------|-----------------|---------------------------------------|
| 0      | 1    | uint8   | level_id        | Level identifier                      |
| 1      | 1    | uint8   | reserved        | Reserved                              |
| 2      | 4    | float32 | resolution_m    | Ground meters per pixel               |
| 6      | 4    | float32 | tile_extent_m   | Ground extent of one tile (meters)    |
| 10     | 2    | uint16  | reserved        | Reserved                              |
| 12     | 8    | float64 | origin_e        | Grid origin easting (top-left)        |
| 20     | 8    | float64 | origin_n        | Grid origin northing (top-left)       |
| 28     | 4    | uint32  | grid_cols       | Number of tile columns                |
| 32     | 4    | uint32  | grid_rows       | Number of tile rows                   |
| 36     | 4    | uint32  | tile_count      | Actual number of non-empty tiles      |
| 40     | 8    | uint64  | index_offset    | Byte offset to this level's index     |
| 48     | 8    | uint64  | index_length    | Length of index in bytes              |
| 56     | 8    | uint64  | data_offset     | Byte offset to this level's tile data |

### Index Entry (8 bytes)

| Offset | Size | Type   | Field  | Description                              |
|--------|------|--------|--------|------------------------------------------|
| 0      | 5    | uint40 | offset | Byte offset relative to level data_offset|
| 5      | 3    | uint24 | length | Tile data length in bytes                |

**Empty tiles**: offset=0, length=0

### Coordinate System

Grid uses top-left origin:
- Column index increases eastward
- Row index increases southward (northing decreases)

```
col = floor((easting - origin_e) / tile_extent_m)
row = floor((origin_n - northing) / tile_extent_m)
```

---

## Swedish Dataset Details

### Orthophoto (Raster)

```
VRT File: Karta_10000_webp_tiled/mosaik.vrt
Raster size: 660,000 × 1,545,000 pixels
CRS: EPSG:3006 (SWEREF 99 TM)
Origin: (265,000, 7,675,000)
Pixel size: 1.0 m
Tile count: 1,919,400
Grid size: 1320 × 3090
Tile size: 500 × 500 px
Image format: WebP
Total size: ~31 GB
Coverage: 47.1% of grid (Sweden shape, not rectangle)
```

### Terrain (Terrain-RGB)

```
VRT File: Karta_Hojd_Sverige_tiled/mosaik.vrt
Raster size: 657,500 × 1,540,000 pixels
CRS: EPSG:3006 (SWEREF 99 TM)
Origin: (265,000, 7,672,500)
Pixel size: 1.0 m
Tile count: 1,892,325
Grid size: 1315 × 3080
Tile size: 500 × 500 px
Image format: PNG
Total size: ~153 GB
Coverage: 46.7% of grid
```

### Geographic Bounds (approximate)

```
Sweden in SWEREF 99 TM:
  Easting:  265,000 → 925,000  (660 km)
  Northing: 6,130,000 → 7,675,000 (1,545 km)
```

---

## Implementation Status

### Completed ✓

1. **Format Specification v2.0** - Complete binary format design
2. **Python Writer v2.1** (`swtiles_writer.py`)
   - Parses GDAL VRT files
   - Builds spatial index
   - Writes SWTILES format
   - `--test N` selects contiguous tile region for testing
   - `--test-region ROW COL` specifies start position
   - `--dry-run` validates without writing

3. **Python Reader v2.2** (`swtiles_reader.py`)
   - Reads SWTILES files
   - `info` - Shows file structure and coverage
   - `debug` - Detailed tile position analysis
   - `tile` - Extracts single tile
   - `mosaic` - Creates spatially correct mosaic image
   - `overview` - Quick grid view of sampled tiles
   - `coverage` - Coverage map visualization
   - `--debug` flag draws row/col labels on tiles

4. **Full Dataset Conversion** - Complete
   - `Karta_10000_webp.swtiles` - 31 GB orthophoto archive
   - `Karta_Hojd_Sverige.swtiles` - 154 GB terrain archive

5. **Cloudflare R2 Upload** - Complete
   - Both datasets uploaded to `tiles` bucket
   - Orthophoto: `Karta_10000_webp.swtiles` (31 GB)
   - Terrain: `Karta_Hojd_Sverige.swtiles` (154 GB)
   - Accessible via HTTP range requests

6. **Cloudflare Worker** (`worker/`) - Access control proxy
   - Origin-based access control (localhost + configured domains)
   - CORS support for browser requests
   - HTTP range request proxying to R2
   - Deployed at `swtiles-proxy.tobias-cornvik.workers.dev`

7. **JavaScript Client** (`webapp/swtiles.js`)
   - Browser-based SWTILES reader
   - Fetches header and level table via range requests
   - Parses binary format (header, level entries, index entries)
   - O(1) tile lookup by row/col or coordinates
   - Coordinate-to-tile and tile-to-bounds conversion

8. **Web Tile Viewer** (`webapp/index.html`)
   - Canvas-based 2D map viewer
   - Pan (drag) and zoom (wheel) navigation
   - Coordinate input for jumping to locations
   - Preset buttons for Swedish cities (Stockholm, Gothenburg, Malmo)
   - Dataset toggle (Orthophoto / Terrain)
   - Per-dataset tile caching
   - Real-time status display (coordinates, row/col, cache stats)

9. **3D Terrain Viewer** (`webapp/viewer3d.html`)
   - Three.js WebGL 3D visualization
   - Loads 5x5 km area (10x10 tiles)
   - Terrain-RGB elevation decoding
   - Orthophoto texture draping on terrain mesh
   - Vertical exaggeration slider (0.5x - 10x)
   - OrbitControls (rotate, pan, zoom)
   - Preset locations: Stockholm, Kebnekaise, Sarek
   - Handles grid offset between terrain/orthophoto datasets

### Not Yet Implemented

1. **Multi-level Support** - Writing multiple resolution levels to single file
2. **Map Library Integration** - Leaflet/MapLibre plugin
3. **Larger 3D Areas** - Progressive loading for bigger regions

---

## Usage

### Writer

```bash
# Validate VRT and source files
python swtiles_writer.py ortho.vrt output.swtiles --dry-run -v

# Create test file with 100 contiguous tiles
python swtiles_writer.py ortho.vrt test.swtiles --test 100

# Create test file starting at specific row/col
python swtiles_writer.py ortho.vrt test.swtiles --test 100 --test-region 500 600

# Full conversion
python swtiles_writer.py ortho.vrt raster.swtiles --data-type raster

# Terrain conversion
python swtiles_writer.py terrain.vrt terrain.swtiles --data-type terrain
```

### Reader

```bash
# File info with coverage analysis
python swtiles_reader.py info test.swtiles

# Debug tile positions
python swtiles_reader.py debug test.swtiles

# Extract single tile
python swtiles_reader.py tile test.swtiles --row 500 --col 600 -o tile.webp
python swtiles_reader.py tile test.swtiles --coord 580000 7240000 -o tile.webp

# Create spatially correct mosaic
python swtiles_reader.py mosaic test.swtiles -o mosaic.png

# Create mosaic with debug labels
python swtiles_reader.py mosaic test.swtiles -o mosaic.png --debug --scale 0.5

# Quick overview grid (not spatial)
python swtiles_reader.py overview test.swtiles -o overview.png

# Coverage map
python swtiles_reader.py coverage test.swtiles -o coverage.png
```

---

## Key Technical Decisions

### Why Custom Format vs MBTiles/PMTiles?

1. **Arbitrary CRS** - MBTiles/PMTiles assume Web Mercator (EPSG:3857)
2. **Flexible Grid** - Not constrained to standard zoom levels
3. **Large Tiles** - 500×500 px tiles vs typical 256×256
4. **Sparse Coverage** - Sweden covers ~47% of bounding box

### Index Design

- Flat array for O(1) lookup: `index[row * cols + col]`
- 8 bytes per entry (5-byte offset + 3-byte length)
- Empty tiles = zeros (no wasted space for missing tiles)
- Index must be fetched first, then tile data (2 HTTP requests)

### Coordinate Convention

- **Top-left origin** with northing decreasing downward
- Matches image coordinate convention (y increases down)
- Row 0 = northernmost tiles
- Col 0 = westernmost tiles

---

## File Sizes

| Component | Size |
|-----------|------|
| Header | 256 bytes |
| Level entry | 64 bytes |
| Index entry | 8 bytes |
| Raster index (1320×3090) | 32.6 MB |
| Terrain index (1315×3080) | 32.4 MB |
| Raster data | ~31 GB |
| Terrain data | ~153 GB |

---

## Next Steps

### Immediate

1. **Deploy Web App** - Host webapp on Cloudflare Pages or similar
2. **Configure Production Domain** - Add domain to worker's ALLOWED_ORIGINS

### Future

1. **Multi-level Pyramid** - Generate lower resolution levels for zoom out
2. **Map Library Integration** - Leaflet/MapLibre plugin for easier integration
3. **Larger 3D Areas** - Progressive loading, LOD, chunked terrain
4. **Compression Analysis** - Compare WebP quality/size tradeoffs

---

## Dependencies

### Python Writer/Reader

```
Python 3.8+
Pillow (PIL) - for image handling in reader
```

Standard library only for writer (no GDAL dependency - parses VRT XML directly).

### JavaScript Client (planned)

```
No dependencies - vanilla fetch with ArrayBuffer
```

---

## Source Files

| File | Purpose | Version |
|------|---------|---------|
| `swtile_writer.py` | VRT → SWTILES converter | v2.1 |
| `swtiles_reader.py` | SWTILES reader + mosaic generator | v2.2 |
| `swetile_spec.md` | Format specification | v2.0 |
| `worker/src/index.ts` | Cloudflare Worker (R2 access control) | v1.0 |
| `worker/wrangler.toml` | Worker configuration (bucket: tiles) | - |
| `webapp/swtiles.js` | JavaScript SWTILES reader | v1.0 |
| `webapp/index.html` | 2D tile viewer with dataset toggle | v1.1 |
| `webapp/viewer3d.html` | Three.js 3D terrain viewer | v1.0 |

---

## Testing Checklist

- [x] Writer parses VRT correctly
- [x] Writer selects contiguous test region
- [x] Writer creates valid SWTILES file
- [x] Reader parses header and level table
- [x] Reader scans tile coverage
- [x] Reader extracts individual tiles
- [x] Reader creates spatially correct mosaic
- [x] Debug mode shows correct row/col placement
- [x] Full dataset conversion (orthophoto + terrain)
- [x] JavaScript client reads via HTTP range requests
- [x] Cloudflare Worker proxies R2 with access control
- [x] 2D viewer displays tiles with pan/zoom
- [x] 2D viewer dataset toggle (orthophoto/terrain)
- [x] R2 performance tested (working)
- [x] 3D viewer loads terrain and orthophoto tiles
- [x] 3D viewer decodes Terrain-RGB elevation
- [x] 3D viewer aligns orthophoto texture with terrain mesh
- [ ] Multi-level pyramid generation
- [ ] Production deployment with custom domain

---

## Contact/Context

This project creates a custom tile format for Swedish national map data from Lantmäteriet. The data is freely available under CC0 license. The goal is efficient browser-based viewing without running a tile server.

Key constraint: Cloudflare R2 (or S3-compatible storage) with HTTP range request support, no server-side processing for tile extraction.
```

---

## Summary

The document above contains everything needed to continue development:

1. **Format specification** - Complete binary layout
2. **Dataset details** - Swedish data parameters
3. **Implementation status** - What's done, what's not
4. **Usage examples** - How to run the tools
5. **Technical decisions** - Why certain choices were made
6. **Next steps** - What to build next

Save this as `PROJECT_STATUS.md` and include the two Python scripts (`swtiles_writer.py` and `swtiles_reader.py`) when starting a new chat.