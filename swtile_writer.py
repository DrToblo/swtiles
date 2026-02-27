#!/usr/bin/env python3
"""
SWTILES Writer v2.1
===================

Converts georeferenced tiles (via VRT) to the SWTILES archive format.

Usage:
    python swtiles_writer.py input.vrt output.swtiles [options]

Options:
    --dry-run           Validate VRT and check all source files exist
    --test N            Process only N tiles (contiguous block) for testing
    --test-region R C   Start test region at row R, col C
    --level ID          Level ID to assign (default: 0)
    --data-type         raster|terrain (default: raster)
    --verbose           Show detailed progress
"""

import argparse
import struct
import sys
import os
import re
import math
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, BinaryIO, Iterator, Tuple, List, Dict
from xml.etree import ElementTree as ET
from enum import IntEnum


# ============================================================================
# Constants
# ============================================================================

MAGIC = b'SWTILES\x00'
VERSION = 2
HEADER_SIZE = 256
LEVEL_ENTRY_SIZE = 64
INDEX_ENTRY_SIZE = 8


class DataType(IntEnum):
    RASTER = 1
    TERRAIN = 2
    OTHER = 3


class ImageFormat(IntEnum):
    WEBP = 1
    PNG = 2
    JPEG = 3
    AVIF = 4


# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class TileSource:
    """Represents a single tile from VRT."""
    filepath: Path
    x_off: int  # Pixel offset X in mosaic
    y_off: int  # Pixel offset Y in mosaic
    row: int = 0  # Computed grid row
    col: int = 0  # Computed grid col


@dataclass 
class LevelConfig:
    """Configuration for a single resolution level."""
    level_id: int
    resolution_m: float
    tile_extent_m: float
    origin_e: float
    origin_n: float
    grid_cols: int
    grid_rows: int
    tile_size_px: int = 500
    tiles: Dict[Tuple[int, int], TileSource] = field(default_factory=dict)

    @property
    def tile_count(self) -> int:
        return len(self.tiles)

    @property
    def index_size(self) -> int:
        return self.grid_cols * self.grid_rows * INDEX_ENTRY_SIZE


@dataclass
class VRTInfo:
    """Parsed VRT file information."""
    filepath: Path
    raster_x_size: int
    raster_y_size: int
    origin_e: float
    origin_n: float
    pixel_size_x: float
    pixel_size_y: float
    crs_epsg: int
    sources: List[TileSource]


# ============================================================================
# Progress Indicator
# ============================================================================

class ProgressBar:
    """Simple progress bar for terminal output."""
    
    def __init__(self, total: int, description: str = "", width: int = 50):
        self.total = total
        self.current = 0
        self.description = description
        self.width = width
        self.last_percent = -1
        
    def update(self, n: int = 1):
        self.current += n
        percent = int(100 * self.current / self.total) if self.total > 0 else 100
        
        if percent != self.last_percent:
            filled = int(self.width * self.current / self.total) if self.total > 0 else self.width
            bar = '█' * filled + '░' * (self.width - filled)
            
            sys.stdout.write(
                f'\r{self.description}: [{bar}] {percent:3d}% ({self.current:,}/{self.total:,})'
            )
            sys.stdout.flush()
            self.last_percent = percent
    
    def finish(self):
        self.current = self.total
        self.update(0)
        print()


class ProgressReporter:
    """Handles progress reporting for different phases."""
    
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        
    def phase(self, name: str):
        print(f"\n{'='*60}")
        print(f"  {name}")
        print(f"{'='*60}")
        
    def info(self, message: str):
        print(f"  ℹ {message}")
        
    def success(self, message: str):
        print(f"  ✓ {message}")
        
    def warning(self, message: str):
        print(f"  ⚠ {message}")
        
    def error(self, message: str):
        print(f"  ✗ {message}")
        
    def detail(self, message: str):
        if self.verbose:
            print(f"    {message}")
            
    def stats(self, label: str, value):
        print(f"  {label:.<40} {value}")
        
    def progress_bar(self, total: int, description: str) -> ProgressBar:
        return ProgressBar(total, f"  {description}")


# ============================================================================
# VRT Parser
# ============================================================================

def parse_geotransform(gt_text: str) -> tuple:
    """Parse GeoTransform string into components."""
    values = [float(x.strip()) for x in gt_text.split(',')]
    return tuple(values)


def extract_epsg(srs_text: str) -> int:
    """Extract EPSG code from SRS string."""
    match = re.search(r'AUTHORITY\["EPSG","(\d+)"\]\]$', srs_text)
    if match:
        return int(match.group(1))
    
    match = re.search(r'EPSG:(\d+)', srs_text)
    if match:
        return int(match.group(1))
    
    if 'SWEREF99' in srs_text or 'SWEREF 99' in srs_text:
        return 3006
        
    raise ValueError(f"Could not extract EPSG code from SRS: {srs_text[:100]}...")


def parse_vrt(vrt_path: Path, progress: ProgressReporter) -> VRTInfo:
    """Parse VRT file and extract tile information."""
    
    progress.info(f"Parsing: {vrt_path}")
    
    tree = ET.parse(vrt_path)
    root = tree.getroot()
    
    raster_x_size = int(root.attrib['rasterXSize'])
    raster_y_size = int(root.attrib['rasterYSize'])
    progress.detail(f"Raster size: {raster_x_size:,} × {raster_y_size:,} pixels")
    
    srs_elem = root.find('SRS')
    if srs_elem is None:
        raise ValueError("VRT missing SRS element")
    crs_epsg = extract_epsg(srs_elem.text)
    progress.detail(f"CRS: EPSG:{crs_epsg}")
    
    gt_elem = root.find('GeoTransform')
    if gt_elem is None:
        raise ValueError("VRT missing GeoTransform element")
    gt = parse_geotransform(gt_elem.text)
    
    origin_e = gt[0]
    pixel_size_x = gt[1]
    origin_n = gt[3]
    pixel_size_y = gt[5]
    
    progress.detail(f"Origin: ({origin_e:,.1f}, {origin_n:,.1f})")
    progress.detail(f"Pixel size: ({pixel_size_x}, {pixel_size_y})")
    
    sources = []
    vrt_dir = vrt_path.parent
    
    all_sources = root.findall('.//SimpleSource')
    progress.detail(f"Found {len(all_sources):,} source entries")
    
    for source in all_sources:
        filename_elem = source.find('SourceFilename')
        dst_rect_elem = source.find('DstRect')
        
        if filename_elem is None or dst_rect_elem is None:
            continue
            
        rel_path = filename_elem.text
        is_relative = filename_elem.attrib.get('relativeToVRT', '0') == '1'
        
        if is_relative:
            filepath = vrt_dir / rel_path
        else:
            filepath = Path(rel_path)
            
        x_off = int(float(dst_rect_elem.attrib['xOff']))
        y_off = int(float(dst_rect_elem.attrib['yOff']))
        
        sources.append(TileSource(
            filepath=filepath,
            x_off=x_off,
            y_off=y_off
        ))
    
    progress.success(f"Parsed {len(sources):,} tile sources")
    
    return VRTInfo(
        filepath=vrt_path,
        raster_x_size=raster_x_size,
        raster_y_size=raster_y_size,
        origin_e=origin_e,
        origin_n=origin_n,
        pixel_size_x=pixel_size_x,
        pixel_size_y=pixel_size_y,
        crs_epsg=crs_epsg,
        sources=sources
    )


# ============================================================================
# Level Builder
# ============================================================================

def build_level_from_vrt(
    vrt_info: VRTInfo,
    level_id: int,
    tile_size_px: int,
    progress: ProgressReporter
) -> LevelConfig:
    """Build level configuration from parsed VRT."""
    
    progress.info("Building level configuration...")
    
    resolution_m = abs(vrt_info.pixel_size_x)
    tile_extent_m = tile_size_px * resolution_m
    
    grid_cols = vrt_info.raster_x_size // tile_size_px
    grid_rows = vrt_info.raster_y_size // tile_size_px
    
    if vrt_info.raster_x_size % tile_size_px != 0:
        grid_cols += 1
        progress.warning(f"Raster width not divisible by tile size, using {grid_cols} columns")
        
    if vrt_info.raster_y_size % tile_size_px != 0:
        grid_rows += 1
        progress.warning(f"Raster height not divisible by tile size, using {grid_rows} rows")
    
    level = LevelConfig(
        level_id=level_id,
        resolution_m=resolution_m,
        tile_extent_m=tile_extent_m,
        origin_e=vrt_info.origin_e,
        origin_n=vrt_info.origin_n,
        grid_cols=grid_cols,
        grid_rows=grid_rows,
        tile_size_px=tile_size_px
    )
    
    # Assign row/col to each tile
    for source in vrt_info.sources:
        col = source.x_off // tile_size_px
        row = source.y_off // tile_size_px
        source.row = row
        source.col = col
        level.tiles[(row, col)] = source
    
    progress.stats("Resolution", f"{resolution_m} m/px")
    progress.stats("Tile extent", f"{tile_extent_m} m")
    progress.stats("Grid size", f"{grid_cols} × {grid_rows}")
    progress.stats("Grid slots", f"{grid_cols * grid_rows:,}")
    progress.stats("Actual tiles", f"{level.tile_count:,}")
    progress.stats("Coverage", f"{100 * level.tile_count / (grid_cols * grid_rows):.1f}%")
    progress.stats("Index size", f"{level.index_size / 1024 / 1024:.1f} MB")
    
    return level


# ============================================================================
# Contiguous Tile Selection
# ============================================================================

def find_dense_region(
    level: LevelConfig,
    target_count: int,
    progress: ProgressReporter
) -> Tuple[int, int, int, int]:
    """
    Find a rectangular region with the most tiles.
    Returns (min_row, min_col, max_row, max_col).
    """
    
    # Calculate approximate square size needed
    side = int(math.ceil(math.sqrt(target_count)))
    
    progress.info(f"Searching for dense {side}×{side} region...")
    
    # Get all tile positions
    all_positions = set(level.tiles.keys())
    
    if not all_positions:
        raise ValueError("No tiles in level")
    
    # Find bounds of existing tiles
    min_row = min(p[0] for p in all_positions)
    max_row = max(p[0] for p in all_positions)
    min_col = min(p[1] for p in all_positions)
    max_col = max(p[1] for p in all_positions)
    
    progress.detail(f"Tiles span rows {min_row}-{max_row}, cols {min_col}-{max_col}")
    
    best_region = None
    best_count = 0
    
    # Slide window to find densest region
    for start_row in range(min_row, max_row - side + 2):
        for start_col in range(min_col, max_col - side + 2):
            count = 0
            for r in range(start_row, min(start_row + side, max_row + 1)):
                for c in range(start_col, min(start_col + side, max_col + 1)):
                    if (r, c) in all_positions:
                        count += 1
            
            if count > best_count:
                best_count = count
                best_region = (
                    start_row, 
                    start_col, 
                    min(start_row + side - 1, max_row),
                    min(start_col + side - 1, max_col)
                )
                
                # Early exit if we found enough
                if best_count >= target_count:
                    break
        
        if best_count >= target_count:
            break
    
    progress.success(f"Found region with {best_count} tiles at "
                     f"rows {best_region[0]}-{best_region[2]}, "
                     f"cols {best_region[1]}-{best_region[3]}")
    
    return best_region


def select_contiguous_tiles(
    level: LevelConfig,
    target_count: int,
    start_row: Optional[int],
    start_col: Optional[int],
    progress: ProgressReporter
) -> Dict[Tuple[int, int], TileSource]:
    """
    Select a contiguous block of tiles.
    
    If start_row/start_col specified, start from there.
    Otherwise, find the densest region.
    """
    
    all_positions = set(level.tiles.keys())
    
    if start_row is not None and start_col is not None:
        # User specified start position
        progress.info(f"Starting from specified position: row={start_row}, col={start_col}")
        
        # Calculate region size
        side = int(math.ceil(math.sqrt(target_count)))
        region = (start_row, start_col, start_row + side - 1, start_col + side - 1)
    else:
        # Find densest region
        region = find_dense_region(level, target_count, progress)
    
    min_row, min_col, max_row, max_col = region
    
    # Collect tiles in region
    selected = {}
    for row in range(min_row, max_row + 1):
        for col in range(min_col, max_col + 1):
            if (row, col) in level.tiles:
                selected[(row, col)] = level.tiles[(row, col)]
                
                if len(selected) >= target_count:
                    break
        if len(selected) >= target_count:
            break
    
    # Report selected region
    if selected:
        sel_rows = [p[0] for p in selected.keys()]
        sel_cols = [p[1] for p in selected.keys()]
        
        progress.info(f"Selected {len(selected)} contiguous tiles:")
        progress.stats("Row range", f"{min(sel_rows)} → {max(sel_rows)}")
        progress.stats("Col range", f"{min(sel_cols)} → {max(sel_cols)}")
        progress.stats("Grid extent", f"{max(sel_cols)-min(sel_cols)+1} × {max(sel_rows)-min(sel_rows)+1}")
        
        # Calculate coordinate bounds
        tile_extent = level.tile_extent_m
        bounds_min_e = level.origin_e + min(sel_cols) * tile_extent
        bounds_max_e = level.origin_e + (max(sel_cols) + 1) * tile_extent
        bounds_max_n = level.origin_n - min(sel_rows) * tile_extent
        bounds_min_n = level.origin_n - (max(sel_rows) + 1) * tile_extent
        
        progress.stats("Bounds E", f"{bounds_min_e:,.0f} → {bounds_max_e:,.0f}")
        progress.stats("Bounds N", f"{bounds_min_n:,.0f} → {bounds_max_n:,.0f}")
    
    return selected


# ============================================================================
# Validation
# ============================================================================

def validate_sources(
    tiles: Dict[Tuple[int, int], TileSource],
    progress: ProgressReporter
) -> Tuple[List[TileSource], List[Path]]:
    """Validate that source files exist and are readable."""
    
    tile_list = list(tiles.values())
    progress.info(f"Validating {len(tile_list):,} source files...")
    
    missing = []
    readable = []
    total_size = 0
    
    bar = progress.progress_bar(len(tile_list), "Checking files")
    
    for tile in tile_list:
        bar.update()
        
        if not tile.filepath.exists():
            missing.append(tile.filepath)
        else:
            try:
                size = tile.filepath.stat().st_size
                total_size += size
                readable.append(tile)
            except OSError:
                missing.append(tile.filepath)
                
    bar.finish()
    
    if missing:
        progress.error(f"{len(missing):,} files missing")
        for p in missing[:10]:
            progress.detail(f"Missing: {p}")
        if len(missing) > 10:
            progress.detail(f"... and {len(missing) - 10} more")
    else:
        progress.success(f"All {len(readable):,} files found")
        
    progress.stats("Total data size", f"{total_size / 1024 / 1024:.2f} MB")
    
    return readable, missing


# ============================================================================
# Binary Writing Helpers
# ============================================================================

def pack_uint40(value: int) -> bytes:
    """Pack integer as 5-byte little-endian."""
    return (value & 0xFFFFFFFFFF).to_bytes(5, 'little')


def pack_uint24(value: int) -> bytes:
    """Pack integer as 3-byte little-endian."""
    return (value & 0xFFFFFF).to_bytes(3, 'little')


def write_header(
    f: BinaryIO,
    data_type: DataType,
    image_format: ImageFormat,
    crs_epsg: int,
    bounds: tuple,
    tile_size_px: int,
    num_levels: int,
    level_table_offset: int
):
    """Write file header."""
    header = bytearray(HEADER_SIZE)
    
    header[0:8] = MAGIC
    struct.pack_into('<H', header, 8, VERSION)
    header[10] = data_type
    header[11] = image_format
    struct.pack_into('<I', header, 12, crs_epsg)
    
    min_e, min_n, max_e, max_n = bounds
    struct.pack_into('<d', header, 16, min_e)
    struct.pack_into('<d', header, 24, min_n)
    struct.pack_into('<d', header, 32, max_e)
    struct.pack_into('<d', header, 40, max_n)
    
    struct.pack_into('<H', header, 48, tile_size_px)
    header[50] = num_levels
    header[51] = 0
    struct.pack_into('<Q', header, 52, level_table_offset)
    
    f.write(header)


def write_level_entry(
    f: BinaryIO,
    level: LevelConfig,
    index_offset: int,
    index_length: int,
    data_offset: int
):
    """Write level table entry."""
    entry = bytearray(LEVEL_ENTRY_SIZE)
    
    entry[0] = level.level_id
    entry[1] = 0
    struct.pack_into('<f', entry, 2, level.resolution_m)
    struct.pack_into('<f', entry, 6, level.tile_extent_m)
    struct.pack_into('<H', entry, 10, 0)
    struct.pack_into('<d', entry, 12, level.origin_e)
    struct.pack_into('<d', entry, 20, level.origin_n)
    struct.pack_into('<I', entry, 28, level.grid_cols)
    struct.pack_into('<I', entry, 32, level.grid_rows)
    struct.pack_into('<I', entry, 36, level.tile_count)
    struct.pack_into('<Q', entry, 40, index_offset)
    struct.pack_into('<Q', entry, 48, index_length)
    struct.pack_into('<Q', entry, 56, data_offset)
    
    f.write(entry)


# ============================================================================
# Main Writer
# ============================================================================

def detect_image_format(filepath: Path) -> ImageFormat:
    """Detect image format from file extension."""
    ext = filepath.suffix.lower()
    mapping = {
        '.webp': ImageFormat.WEBP,
        '.png': ImageFormat.PNG,
        '.jpg': ImageFormat.JPEG,
        '.jpeg': ImageFormat.JPEG,
        '.avif': ImageFormat.AVIF,
    }
    return mapping.get(ext, ImageFormat.PNG)


def write_swtiles(
    output_path: Path,
    levels: List[LevelConfig],
    crs_epsg: int,
    data_type: DataType,
    progress: ProgressReporter
):
    """Write complete SWTILES file."""
    
    progress.phase("Writing SWTILES")
    
    # Sort levels coarsest first (highest resolution_m first)
    levels = sorted(levels, key=lambda l: -l.resolution_m)
    
    # Detect image format from first tile
    first_tile = next(iter(levels[0].tiles.values()))
    image_format = detect_image_format(first_tile.filepath)
    progress.info(f"Image format: {image_format.name}")
    
    # Calculate overall bounds from actual tiles
    all_bounds = []
    for level in levels:
        if level.tiles:
            rows = [p[0] for p in level.tiles.keys()]
            cols = [p[1] for p in level.tiles.keys()]
            
            min_e = level.origin_e + min(cols) * level.tile_extent_m
            max_e = level.origin_e + (max(cols) + 1) * level.tile_extent_m
            max_n = level.origin_n - min(rows) * level.tile_extent_m
            min_n = level.origin_n - (max(rows) + 1) * level.tile_extent_m
            
            all_bounds.append((min_e, min_n, max_e, max_n))
    
    if all_bounds:
        bounds = (
            min(b[0] for b in all_bounds),
            min(b[1] for b in all_bounds),
            max(b[2] for b in all_bounds),
            max(b[3] for b in all_bounds)
        )
    else:
        bounds = (0, 0, 0, 0)
    
    progress.info(f"Bounds: E({bounds[0]:.0f}-{bounds[2]:.0f}) N({bounds[1]:.0f}-{bounds[3]:.0f})")
    
    # Calculate offsets
    level_table_offset = HEADER_SIZE
    level_table_length = len(levels) * LEVEL_ENTRY_SIZE
    
    current_offset = level_table_offset + level_table_length
    
    level_offsets = []
    for level in levels:
        index_offset = current_offset
        index_length = level.index_size
        data_offset = index_offset + index_length
        level_offsets.append({
            'index_offset': index_offset,
            'index_length': index_length,
            'data_offset': data_offset,
        })
        current_offset = data_offset
    
    with open(output_path, 'wb') as f:
        progress.info(f"Output: {output_path}")
        
        # Write placeholder header
        f.write(b'\x00' * HEADER_SIZE)
        
        # Write placeholder level table
        f.write(b'\x00' * level_table_length)
        
        # Write each level
        for level_idx, level in enumerate(levels):
            progress.info(f"\nLevel {level.level_id} (resolution: {level.resolution_m} m/px)")
            
            offsets = level_offsets[level_idx]
            index_offset = offsets['index_offset']
            data_offset = offsets['data_offset']
            
            tiles_to_write = list(level.tiles.items())
            
            # Sort tiles by row, then col for sequential disk access
            tiles_to_write.sort(key=lambda x: (x[0][0], x[0][1]))
            
            # Build index and write data
            index = bytearray(level.index_size)
            
            f.seek(data_offset)
            current_data_offset = 0
            
            bar = progress.progress_bar(len(tiles_to_write), "Writing tiles")
            
            for (row, col), tile in tiles_to_write:
                bar.update()
                
                try:
                    tile_data = tile.filepath.read_bytes()
                except OSError as e:
                    progress.error(f"Failed to read {tile.filepath}: {e}")
                    continue
                
                f.write(tile_data)
                
                entry_idx = row * level.grid_cols + col
                entry_offset = entry_idx * INDEX_ENTRY_SIZE
                
                index[entry_offset:entry_offset + 5] = pack_uint40(current_data_offset)
                index[entry_offset + 5:entry_offset + 8] = pack_uint24(len(tile_data))
                
                current_data_offset += len(tile_data)
            
            bar.finish()
            
            offsets['data_length'] = current_data_offset
            
            if level_idx + 1 < len(levels):
                next_start = data_offset + current_data_offset
                level_offsets[level_idx + 1]['index_offset'] = next_start
                level_offsets[level_idx + 1]['data_offset'] = next_start + levels[level_idx + 1].index_size
            
            # Write index
            f.seek(index_offset)
            f.write(index)
            
            f.seek(data_offset + current_data_offset)
            
            progress.stats(f"Level {level.level_id} tiles written", f"{len(tiles_to_write):,}")
            progress.stats(f"Level {level.level_id} data size", f"{current_data_offset / 1024 / 1024:.1f} MB")
        
        final_size = f.tell()
        
        # Rewrite header
        f.seek(0)
        write_header(
            f,
            data_type=data_type,
            image_format=image_format,
            crs_epsg=crs_epsg,
            bounds=bounds,
            tile_size_px=levels[0].tile_size_px,
            num_levels=len(levels),
            level_table_offset=level_table_offset
        )
        
        # Rewrite level table
        f.seek(level_table_offset)
        for level_idx, level in enumerate(levels):
            offsets = level_offsets[level_idx]
            write_level_entry(
                f,
                level,
                index_offset=offsets['index_offset'],
                index_length=offsets['index_length'],
                data_offset=offsets['data_offset']
            )
    
    progress.success(f"Written: {output_path}")
    progress.stats("Final file size", f"{final_size / 1024 / 1024:.2f} MB")


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Convert VRT tiles to SWTILES format',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run to validate files
  python swtiles_writer.py ortho.vrt output.swtiles --dry-run

  # Test with 100 contiguous tiles
  python swtiles_writer.py ortho.vrt output.swtiles --test 100

  # Test with tiles starting at specific location
  python swtiles_writer.py ortho.vrt output.swtiles --test 100 --test-region 500 600

  # Full conversion
  python swtiles_writer.py ortho.vrt raster.swtiles --data-type raster
        """
    )
    
    parser.add_argument('input_vrt', type=Path, help='Input VRT file')
    parser.add_argument('output', type=Path, help='Output SWTILES file')
    parser.add_argument('--dry-run', action='store_true',
                        help='Validate without writing output')
    parser.add_argument('--test', type=int, metavar='N',
                        help='Process only N contiguous tiles for testing')
    parser.add_argument('--test-region', type=int, nargs=2, metavar=('ROW', 'COL'),
                        help='Start test region at specific row/col')
    parser.add_argument('--level', type=int, default=0,
                        help='Level ID to assign (default: 0)')
    parser.add_argument('--data-type', choices=['raster', 'terrain'], default='raster',
                        help='Data type (default: raster)')
    parser.add_argument('--tile-size', type=int, default=500,
                        help='Tile size in pixels (default: 500)')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Show detailed output')
    
    args = parser.parse_args()
    
    progress = ProgressReporter(verbose=args.verbose)
    
    print("\n" + "="*60)
    print("  SWTILES Writer v2.1")
    print("="*60)
    
    if not args.input_vrt.exists():
        progress.error(f"Input VRT not found: {args.input_vrt}")
        sys.exit(1)
    
    # Parse VRT
    progress.phase("Parsing VRT")
    try:
        vrt_info = parse_vrt(args.input_vrt, progress)
    except Exception as e:
        progress.error(f"Failed to parse VRT: {e}")
        sys.exit(1)
    
    # Build level configuration
    progress.phase("Building Level Configuration")
    level = build_level_from_vrt(
        vrt_info,
        level_id=args.level,
        tile_size_px=args.tile_size,
        progress=progress
    )
    
    # Select tiles (contiguous subset for test mode)
    if args.test:
        progress.phase("Selecting Contiguous Test Region")
        
        start_row = args.test_region[0] if args.test_region else None
        start_col = args.test_region[1] if args.test_region else None
        
        selected_tiles = select_contiguous_tiles(
            level,
            target_count=args.test,
            start_row=start_row,
            start_col=start_col,
            progress=progress
        )
        
        # Replace level tiles with selection
        level.tiles = selected_tiles
    
    # Validate source files
    progress.phase("Validating Source Files")
    valid_tiles, missing_tiles = validate_sources(level.tiles, progress)
    
    if missing_tiles:
        if args.dry_run:
            progress.error(f"Dry run failed: {len(missing_tiles)} files missing")
            sys.exit(1)
        else:
            progress.warning(f"Proceeding with {len(valid_tiles)} valid tiles")
            level.tiles = {(t.row, t.col): t for t in valid_tiles}
    
    if args.dry_run:
        progress.phase("Dry Run Complete")
        progress.success("All validations passed")
        progress.stats("Ready to write", f"{level.tile_count:,} tiles")
        
        if valid_tiles:
            total_input = sum(t.filepath.stat().st_size for t in valid_tiles)
            est_size = total_input + level.index_size + HEADER_SIZE + LEVEL_ENTRY_SIZE
            progress.stats("Estimated output", f"{est_size / 1024 / 1024:.1f} MB")
        
        print("\nRun without --dry-run to create output file.")
        sys.exit(0)
    
    # Write output
    data_type = DataType.RASTER if args.data_type == 'raster' else DataType.TERRAIN
    
    write_swtiles(
        output_path=args.output,
        levels=[level],
        crs_epsg=vrt_info.crs_epsg,
        data_type=data_type,
        progress=progress
    )
    
    progress.phase("Complete")
    progress.success("SWTILES file created successfully")


if __name__ == '__main__':
    main()