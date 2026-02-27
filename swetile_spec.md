# SWTILES Format Specification & Python Writer

## Part 1: Format Specification Document

```markdown
# SWTILES Format Specification
## Version 2.0

### Overview

SWTILES is a binary tile archive format designed for storing georeferenced 
raster tiles in arbitrary coordinate reference systems with support for 
multiple resolution levels. It is optimized for HTTP range-request access 
patterns, making it suitable for cloud storage (e.g., Cloudflare R2, S3).

### Design Goals

- Single-file archive containing multiple resolution levels
- O(1) tile lookup by coordinate or grid position
- HTTP range-request friendly (minimal requests per tile fetch)
- Self-describing (all parameters stored in file)
- Arbitrary CRS support (not limited to Web Mercator)
- Flexible resolution levels (no assumed pyramid structure)

---

## File Structure

```
┌─────────────────────────────────────────────────────────────────┐
│ FILE HEADER (256 bytes)                                         │
├─────────────────────────────────────────────────────────────────┤
│ LEVEL TABLE (64 bytes × num_levels)                            │
├─────────────────────────────────────────────────────────────────┤
│ LEVEL SECTIONS (repeated for each level, coarsest first)       │
│   ├── Level N Index                                            │
│   ├── Level N Tile Data                                        │
│   ├── Level N-1 Index                                          │
│   ├── Level N-1 Tile Data                                      │
│   └── ... through Level 0 (finest)                             │
└─────────────────────────────────────────────────────────────────┘
```

---

## File Header (256 bytes)

| Offset | Size | Type    | Field              | Description                          |
|--------|------|---------|--------------------|--------------------------------------|
| 0      | 8    | char[8] | magic              | "SWTILES\0"                          |
| 8      | 2    | uint16  | version            | Format version (currently 2)         |
| 10     | 1    | uint8   | data_type          | 1=raster, 2=terrain, 3=other        |
| 11     | 1    | uint8   | image_format       | 1=webp, 2=png, 3=jpg, 4=avif        |
| 12     | 4    | uint32  | crs_epsg           | EPSG code (e.g., 3006)              |
| 16     | 8    | float64 | bounds_min_e       | Minimum easting (all levels)         |
| 24     | 8    | float64 | bounds_min_n       | Minimum northing (all levels)        |
| 32     | 8    | float64 | bounds_max_e       | Maximum easting (all levels)         |
| 40     | 8    | float64 | bounds_max_n       | Maximum northing (all levels)        |
| 48     | 2    | uint16  | tile_size_px       | Tile dimension in pixels (e.g., 500) |
| 50     | 1    | uint8   | num_levels         | Number of resolution levels (1-255)  |
| 51     | 1    | uint8   | reserved_1         | Reserved, must be 0                  |
| 52     | 8    | uint64  | level_table_offset | Byte offset to level table           |
| 60     | 196  | bytes   | reserved           | Reserved, must be 0                  |

**Total: 256 bytes**

### Field Details

**magic**: Must be exactly `SWTILES\0` (8 bytes including null terminator).

**version**: Current version is 2. Readers should reject files with 
unsupported versions.

**data_type**:
- 1 = Raster imagery (orthophoto, satellite)
- 2 = Terrain (elevation data, e.g., Mapbox Terrain-RGB)
- 3 = Other/custom

**image_format**:
- 1 = WebP
- 2 = PNG
- 3 = JPEG
- 4 = AVIF

**bounds_***: Bounding box encompassing all tiles across all levels, 
in the file's CRS. Used for quick rejection of out-of-bounds queries.

**tile_size_px**: All tiles in the file must have this dimension 
(width = height = tile_size_px).

---

## Level Table Entry (64 bytes per level)

| Offset | Size | Type    | Field           | Description                           |
|--------|------|---------|-----------------|---------------------------------------|
| 0      | 1    | uint8   | level_id        | Level identifier (arbitrary, unique)  |
| 1      | 1    | uint8   | reserved_1      | Reserved                              |
| 2      | 4    | float32 | resolution_m    | Ground meters per pixel               |
| 6      | 4    | float32 | tile_extent_m   | Ground extent of one tile (meters)    |
| 10     | 2    | uint16  | reserved_2      | Reserved                              |
| 12     | 8    | float64 | origin_e        | Grid origin easting (top-left)        |
| 20     | 8    | float64 | origin_n        | Grid origin northing (top-left)       |
| 28     | 4    | uint32  | grid_cols       | Number of tile columns                |
| 32     | 4    | uint32  | grid_rows       | Number of tile rows                   |
| 36     | 4    | uint32  | tile_count      | Actual number of non-empty tiles      |
| 40     | 8    | uint64  | index_offset    | Byte offset to this level's index     |
| 48     | 8    | uint64  | index_length    | Length of index in bytes              |
| 56     | 8    | uint64  | data_offset     | Byte offset to this level's tile data |

**Total: 64 bytes per level**

### Coordinate System

The grid uses a top-left origin with:
- Column index increases eastward
- Row index increases southward (northing decreases)

```
Origin (origin_e, origin_n)
    ┌────┬────┬────┬────┐
    │0,0 │0,1 │0,2 │0,3 │  ← Row 0
    ├────┼────┼────┼────┤
    │1,0 │1,1 │1,2 │1,3 │  ← Row 1
    ├────┼────┼────┼────┤
    │2,0 │2,1 │2,2 │2,3 │  ← Row 2
    └────┴────┴────┴────┘
      ↑
    Col 0
```

### Coordinate to Grid Conversion

```
col = floor((easting - origin_e) / tile_extent_m)
row = floor((origin_n - northing) / tile_extent_m)
```

### Grid to Coordinate Conversion (tile bounds)

```
min_e = origin_e + col * tile_extent_m
max_e = min_e + tile_extent_m
max_n = origin_n - row * tile_extent_m
min_n = max_n - tile_extent_m
```

---

## Level Index

The index for each level is a flat array of 8-byte entries, one per grid cell,
in row-major order:

```
index[row * grid_cols + col] = (offset: uint40, length: uint24)
```

| Offset | Size | Type   | Field  | Description                              |
|--------|------|--------|--------|------------------------------------------|
| 0      | 5    | uint40 | offset | Byte offset relative to level data_offset|
| 5      | 3    | uint24 | length | Tile data length in bytes                |

**Total: 8 bytes per entry**

**Empty tiles**: offset=0, length=0

**Maximum addressable**: 
- Offset: 2^40 = 1 TB per level
- Length: 2^24 = 16 MB per tile

### Index Size Calculation

```
index_length = grid_cols * grid_rows * 8 bytes
```

---

## Tile Data

Tile data for each level is stored as concatenated image blobs (WebP, PNG, etc.)
in the order they were written. The index provides the offset and length for
each tile.

Tiles are stored as raw image file bytes with no additional framing or headers.

---

## File Layout Example

```
Offset      Content
──────────────────────────────────────────────────────────
0x00000000  File Header (256 bytes)
0x00000100  Level Table Entry 0 (64 bytes) - coarsest
0x00000140  Level Table Entry 1 (64 bytes)
0x00000180  Level Table Entry 2 (64 bytes) - finest
0x000001C0  Level 0 Index (e.g., 800 bytes for 10×10 grid)
0x000004E0  Level 0 Tile Data
0x00050000  Level 1 Index
0x00055000  Level 1 Tile Data
0x00800000  Level 2 Index
0x00850000  Level 2 Tile Data
...         EOF
```

---

## Reading Algorithm

### Initialization

1. Read header (bytes 0-255)
2. Validate magic and version
3. Read level table (bytes 256 to 256 + num_levels * 64)
4. Optionally cache coarse level indexes

### Tile Fetch by Coordinate

1. Select appropriate level based on desired resolution
2. Convert coordinate to (row, col) using level's origin and extent
3. Validate row/col within grid bounds
4. Calculate index entry position:
   `index_offset + (row * grid_cols + col) * 8`
5. Read 8-byte index entry
6. If length == 0, tile is empty (return null)
7. Read tile data from `data_offset + offset` with `length` bytes

---

## Versioning

- Version 1: Reserved (never released)
- Version 2: Current specification

Future versions should maintain backward compatibility for the header
structure through byte 60 (level_table_offset).

---

## MIME Type

Suggested: `application/x-swtiles`

## File Extension

`.swtiles`

---

## Reference Implementations

- Python: swtiles-py (writer and reader)
- JavaScript: swtiles-js (reader with HTTP range requests)

---

## Changelog

- 2024-01-XX: Version 2.0 - Initial public specification
```

---
