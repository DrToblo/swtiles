#!/usr/bin/env python3
"""
SWTILES Reader v2.2
===================

Read tiles from SWTILES archive and generate mosaic images for verification.
Now with debug mode to verify tile placement.

Usage:
    python swtiles_reader.py info raster.swtiles
    python swtiles_reader.py mosaic raster.swtiles -o mosaic.png --debug
    python swtiles_reader.py overview raster.swtiles -o overview.png
"""

import argparse
import struct
import sys
import io
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, BinaryIO, Tuple, List, Iterator, Callable, Dict
from enum import IntEnum


# ============================================================================
# Optional imports for image handling
# ============================================================================

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("Warning: PIL not installed. Install with: pip install Pillow")


# ============================================================================
# Constants (must match writer)
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
class FileHeader:
    """Parsed file header."""
    magic: bytes
    version: int
    data_type: DataType
    image_format: ImageFormat
    crs_epsg: int
    bounds_min_e: float
    bounds_min_n: float
    bounds_max_e: float
    bounds_max_n: float
    tile_size_px: int
    num_levels: int
    level_table_offset: int


@dataclass
class LevelInfo:
    """Parsed level table entry."""
    level_id: int
    resolution_m: float
    tile_extent_m: float
    origin_e: float
    origin_n: float
    grid_cols: int
    grid_rows: int
    tile_count: int
    index_offset: int
    index_length: int
    data_offset: int


@dataclass
class CoverageInfo:
    """Coverage statistics for a level."""
    non_empty_count: int
    total_size: int
    row_range: Optional[Tuple[int, int]]
    col_range: Optional[Tuple[int, int]]
    bounds: Optional[Tuple[float, float, float, float]]
    grid_extent: Optional[Tuple[int, int]]
    tile_positions: List[Tuple[int, int]]  # Added: list of (row, col)


# ============================================================================
# Progress Bar
# ============================================================================

class ProgressBar:
    """Simple progress bar for terminal output."""
    
    def __init__(self, description: str = "", width: int = 50):
        self.description = description
        self.width = width
        self.last_percent = -1
    
    def update(self, current: int, total: int):
        percent = int(100 * current / total) if total > 0 else 100
        
        if percent != self.last_percent:
            filled = int(self.width * current / total) if total > 0 else self.width
            bar = '█' * filled + '░' * (self.width - filled)
            sys.stdout.write(
                f'\r  {self.description}: [{bar}] {percent:3d}% ({current:,}/{total:,})'
            )
            sys.stdout.flush()
            self.last_percent = percent
    
    def finish(self):
        print()


# ============================================================================
# Reader Class
# ============================================================================

class SwtilesReader:
    """Reader for SWTILES format files."""
    
    def __init__(self, filepath: Path):
        self.filepath = Path(filepath)
        self.file: Optional[BinaryIO] = None
        self.header: Optional[FileHeader] = None
        self.levels: List[LevelInfo] = []
        
        self._open()
        self._read_header()
        self._read_level_table()
    
    def _open(self):
        """Open file for reading."""
        if not self.filepath.exists():
            raise FileNotFoundError(f"File not found: {self.filepath}")
        self.file = open(self.filepath, 'rb')
    
    def close(self):
        """Close file."""
        if self.file:
            self.file.close()
            self.file = None
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
    
    def _read_header(self):
        """Parse file header."""
        self.file.seek(0)
        data = self.file.read(HEADER_SIZE)
        
        if len(data) < HEADER_SIZE:
            raise ValueError("File too small for header")
        
        magic = data[0:8]
        if magic != MAGIC:
            raise ValueError(f"Invalid magic: {magic}, expected {MAGIC}")
        
        version = struct.unpack_from('<H', data, 8)[0]
        if version != VERSION:
            raise ValueError(f"Unsupported version: {version}, expected {VERSION}")
        
        self.header = FileHeader(
            magic=magic,
            version=version,
            data_type=DataType(data[10]),
            image_format=ImageFormat(data[11]),
            crs_epsg=struct.unpack_from('<I', data, 12)[0],
            bounds_min_e=struct.unpack_from('<d', data, 16)[0],
            bounds_min_n=struct.unpack_from('<d', data, 24)[0],
            bounds_max_e=struct.unpack_from('<d', data, 32)[0],
            bounds_max_n=struct.unpack_from('<d', data, 40)[0],
            tile_size_px=struct.unpack_from('<H', data, 48)[0],
            num_levels=data[50],
            level_table_offset=struct.unpack_from('<Q', data, 52)[0]
        )
    
    def _read_level_table(self):
        """Parse level table."""
        self.file.seek(self.header.level_table_offset)
        
        for _ in range(self.header.num_levels):
            data = self.file.read(LEVEL_ENTRY_SIZE)
            
            level = LevelInfo(
                level_id=data[0],
                resolution_m=struct.unpack_from('<f', data, 2)[0],
                tile_extent_m=struct.unpack_from('<f', data, 6)[0],
                origin_e=struct.unpack_from('<d', data, 12)[0],
                origin_n=struct.unpack_from('<d', data, 20)[0],
                grid_cols=struct.unpack_from('<I', data, 28)[0],
                grid_rows=struct.unpack_from('<I', data, 32)[0],
                tile_count=struct.unpack_from('<I', data, 36)[0],
                index_offset=struct.unpack_from('<Q', data, 40)[0],
                index_length=struct.unpack_from('<Q', data, 48)[0],
                data_offset=struct.unpack_from('<Q', data, 56)[0]
            )
            self.levels.append(level)
    
    def get_level(self, level_id: int = 0) -> LevelInfo:
        """Get level by ID."""
        for level in self.levels:
            if level.level_id == level_id:
                return level
        raise ValueError(f"Level {level_id} not found")
    
    def get_finest_level(self) -> LevelInfo:
        """Get finest resolution level."""
        return min(self.levels, key=lambda l: l.resolution_m)
    
    def get_coarsest_level(self) -> LevelInfo:
        """Get coarsest resolution level."""
        return max(self.levels, key=lambda l: l.resolution_m)
    
    def coord_to_rowcol(self, easting: float, northing: float,
                        level: LevelInfo) -> Tuple[int, int]:
        """Convert coordinate to row/col for given level."""
        col = int((easting - level.origin_e) / level.tile_extent_m)
        row = int((level.origin_n - northing) / level.tile_extent_m)
        return row, col
    
    def rowcol_to_bounds(self, row: int, col: int,
                         level: LevelInfo) -> Tuple[float, float, float, float]:
        """Get coordinate bounds for tile at row/col."""
        min_e = level.origin_e + col * level.tile_extent_m
        max_e = min_e + level.tile_extent_m
        max_n = level.origin_n - row * level.tile_extent_m
        min_n = max_n - level.tile_extent_m
        return (min_e, min_n, max_e, max_n)
    
    def _read_index_entry(self, row: int, col: int,
                          level: LevelInfo) -> Tuple[int, int]:
        """Read single index entry, return (offset, length)."""
        if row < 0 or row >= level.grid_rows or col < 0 or col >= level.grid_cols:
            return (0, 0)
        
        entry_idx = row * level.grid_cols + col
        entry_offset = level.index_offset + entry_idx * INDEX_ENTRY_SIZE
        
        self.file.seek(entry_offset)
        entry = self.file.read(INDEX_ENTRY_SIZE)
        
        offset = int.from_bytes(entry[0:5], 'little')
        length = int.from_bytes(entry[5:8], 'little')
        
        return offset, length
    
    def read_tile(self, row: int, col: int,
                  level: Optional[LevelInfo] = None) -> Optional[bytes]:
        """Read tile data by row/col."""
        if level is None:
            level = self.get_finest_level()
        
        offset, length = self._read_index_entry(row, col, level)
        
        if length == 0:
            return None
        
        self.file.seek(level.data_offset + offset)
        return self.file.read(length)
    
    def read_tile_as_image(self, row: int, col: int,
                           level: Optional[LevelInfo] = None) -> Optional['Image.Image']:
        """Read tile and decode as PIL Image."""
        if not HAS_PIL:
            raise RuntimeError("PIL not installed")
        
        data = self.read_tile(row, col, level)
        if data is None:
            return None
        
        return Image.open(io.BytesIO(data))
    
    def get_tile_coverage(self, level: Optional[LevelInfo] = None,
                          progress_callback: Optional[Callable[[int, int], None]] = None
                          ) -> CoverageInfo:
        """
        Scan index to find actual tile coverage.
        Returns CoverageInfo with statistics and list of tile positions.
        """
        if level is None:
            level = self.get_finest_level()
        
        min_row, max_row = level.grid_rows, 0
        min_col, max_col = level.grid_cols, 0
        non_empty_count = 0
        total_size = 0
        tile_positions = []
        
        self.file.seek(level.index_offset)
        index_data = self.file.read(level.index_length)
        
        total_entries = level.grid_rows * level.grid_cols
        
        for row in range(level.grid_rows):
            for col in range(level.grid_cols):
                if progress_callback:
                    entry_num = row * level.grid_cols + col
                    if entry_num % 100000 == 0:
                        progress_callback(entry_num, total_entries)
                
                entry_idx = row * level.grid_cols + col
                entry_offset = entry_idx * INDEX_ENTRY_SIZE
                
                offset = int.from_bytes(index_data[entry_offset:entry_offset + 5], 'little')
                length = int.from_bytes(index_data[entry_offset + 5:entry_offset + 8], 'little')
                
                if length > 0:
                    non_empty_count += 1
                    total_size += length
                    min_row = min(min_row, row)
                    max_row = max(max_row, row)
                    min_col = min(min_col, col)
                    max_col = max(max_col, col)
                    tile_positions.append((row, col))
        
        if progress_callback:
            progress_callback(total_entries, total_entries)
        
        if non_empty_count == 0:
            return CoverageInfo(
                non_empty_count=0,
                total_size=0,
                row_range=None,
                col_range=None,
                bounds=None,
                grid_extent=None,
                tile_positions=[]
            )
        
        bounds = (
            level.origin_e + min_col * level.tile_extent_m,
            level.origin_n - (max_row + 1) * level.tile_extent_m,
            level.origin_e + (max_col + 1) * level.tile_extent_m,
            level.origin_n - min_row * level.tile_extent_m
        )
        
        return CoverageInfo(
            non_empty_count=non_empty_count,
            total_size=total_size,
            row_range=(min_row, max_row),
            col_range=(min_col, max_col),
            bounds=bounds,
            grid_extent=(max_col - min_col + 1, max_row - min_row + 1),
            tile_positions=tile_positions
        )
    
    def iter_non_empty_tiles(self, level: Optional[LevelInfo] = None
                             ) -> Iterator[Tuple[int, int, int, int]]:
        """Iterate over all non-empty tiles, yielding (row, col, offset, length)."""
        if level is None:
            level = self.get_finest_level()
        
        self.file.seek(level.index_offset)
        index_data = self.file.read(level.index_length)
        
        for row in range(level.grid_rows):
            for col in range(level.grid_cols):
                entry_idx = row * level.grid_cols + col
                entry_offset = entry_idx * INDEX_ENTRY_SIZE
                
                offset = int.from_bytes(index_data[entry_offset:entry_offset + 5], 'little')
                length = int.from_bytes(index_data[entry_offset + 5:entry_offset + 8], 'little')
                
                if length > 0:
                    yield row, col, offset, length
    
    def count_tiles_in_bounds(self, bounds: Tuple[float, float, float, float],
                              level: Optional[LevelInfo] = None
                              ) -> Tuple[int, int]:
        """Count total and non-empty tiles in bounds. Returns (total, non_empty)."""
        if level is None:
            level = self.get_finest_level()
        
        min_e, min_n, max_e, max_n = bounds
        
        min_row, _ = self.coord_to_rowcol(min_e, max_n, level)
        max_row, _ = self.coord_to_rowcol(max_e, min_n, level)
        _, min_col = self.coord_to_rowcol(min_e, max_n, level)
        _, max_col = self.coord_to_rowcol(max_e, min_n, level)
        
        min_row = max(0, min_row)
        max_row = min(level.grid_rows - 1, max_row)
        min_col = max(0, min_col)
        max_col = min(level.grid_cols - 1, max_col)
        
        total = 0
        non_empty = 0
        
        for row in range(min_row, max_row + 1):
            for col in range(min_col, max_col + 1):
                total += 1
                offset, length = self._read_index_entry(row, col, level)
                if length > 0:
                    non_empty += 1
        
        return total, non_empty


# ============================================================================
# Mosaic Generator
# ============================================================================

class MosaicGenerator:
    """Generate mosaic images from SWTILES."""
    
    def __init__(self, reader: SwtilesReader, debug: bool = False):
        self.reader = reader
        self.debug = debug
    
    def _draw_debug_label(self, img: 'Image.Image', row: int, col: int) -> 'Image.Image':
        """Draw row/col label on tile for debugging."""
        if not self.debug:
            return img
        
        img = img.copy()
        draw = ImageDraw.Draw(img)
        
        label = f"r{row}\nc{col}"
        
        # Draw background rectangle
        bbox = draw.textbbox((5, 5), label)
        draw.rectangle([bbox[0]-2, bbox[1]-2, bbox[2]+2, bbox[3]+2], 
                       fill=(0, 0, 0, 180))
        
        # Draw text
        draw.text((5, 5), label, fill=(255, 255, 0))
        
        # Draw border
        draw.rectangle([0, 0, img.width-1, img.height-1], outline=(255, 0, 0), width=2)
        
        return img
    
    def create_spatial_mosaic(
        self,
        level: Optional[LevelInfo] = None,
        coverage: Optional[CoverageInfo] = None,
        max_tiles: int = 10000,
        background_color: Tuple[int, int, int] = (64, 64, 64),
        scale: float = 1.0,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> Tuple['Image.Image', Dict]:
        """
        Create a spatially correct mosaic from existing tiles.
        
        Tiles are placed at their correct row/col positions.
        
        Returns:
            Tuple of (PIL Image, debug_info dict)
        """
        if not HAS_PIL:
            raise RuntimeError("PIL not installed")
        
        if level is None:
            level = self.reader.get_finest_level()
        
        if coverage is None:
            coverage = self.reader.get_tile_coverage(level)
        
        if coverage.non_empty_count == 0:
            raise ValueError("No tiles found")
        
        if coverage.non_empty_count > max_tiles:
            raise ValueError(
                f"Too many tiles ({coverage.non_empty_count}). "
                f"Increase max_tiles or use a smaller region."
            )
        
        # Calculate output dimensions based on tile positions
        min_row, max_row = coverage.row_range
        min_col, max_col = coverage.col_range
        
        num_cols = max_col - min_col + 1
        num_rows = max_row - min_row + 1
        
        tile_size = self.reader.header.tile_size_px
        output_tile_size = int(tile_size * scale)
        
        output_width = num_cols * output_tile_size
        output_height = num_rows * output_tile_size
        
        debug_info = {
            'min_row': min_row,
            'max_row': max_row,
            'min_col': min_col,
            'max_col': max_col,
            'num_rows': num_rows,
            'num_cols': num_cols,
            'tile_size': tile_size,
            'output_tile_size': output_tile_size,
            'output_width': output_width,
            'output_height': output_height,
            'tiles_placed': [],
        }
        
        print(f"\n    Debug info:")
        print(f"      Row range: {min_row} → {max_row} ({num_rows} rows)")
        print(f"      Col range: {min_col} → {max_col} ({num_cols} cols)")
        print(f"      Output size: {output_width} × {output_height} px")
        print(f"      Tile size: {output_tile_size} px (scale={scale})")
        
        # Create output image
        mosaic = Image.new('RGB', (output_width, output_height), background_color)
        
        # Place each tile at its correct position
        total = len(coverage.tile_positions)
        
        for i, (row, col) in enumerate(coverage.tile_positions):
            if progress_callback:
                progress_callback(i + 1, total)
            
            tile_img = self.reader.read_tile_as_image(row, col, level)
            
            if tile_img is not None:
                if tile_img.mode != 'RGB':
                    tile_img = tile_img.convert('RGB')
                
                # Scale if needed
                if scale != 1.0:
                    tile_img = tile_img.resize(
                        (output_tile_size, output_tile_size),
                        Image.Resampling.LANCZOS
                    )
                
                # Add debug overlay
                tile_img = self._draw_debug_label(tile_img, row, col)
                
                # Calculate position: RELATIVE to min_row/min_col
                x = (col - min_col) * output_tile_size
                y = (row - min_row) * output_tile_size
                
                if self.debug and i < 5:
                    print(f"      Tile (row={row}, col={col}) → pixel ({x}, {y})")
                
                debug_info['tiles_placed'].append({
                    'row': row,
                    'col': col,
                    'x': x,
                    'y': y
                })
                
                mosaic.paste(tile_img, (x, y))
        
        if self.debug:
            print(f"      ... placed {len(debug_info['tiles_placed'])} tiles total")
        
        return mosaic, debug_info
    
    def create_overview_grid(
        self,
        max_samples: int = 400,
        level: Optional[LevelInfo] = None,
        tile_display_size: int = 100,
        grid_cols: int = 20,
        background_color: Tuple[int, int, int] = (64, 64, 64),
        border_color: Tuple[int, int, int] = (32, 32, 32),
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> 'Image.Image':
        """
        Create overview grid showing samples of tiles.
        
        NOTE: This is NOT a spatial mosaic - tiles are arranged in a grid
        for quick visual inspection, not geographic position.
        """
        if not HAS_PIL:
            raise RuntimeError("PIL not installed")
        
        if level is None:
            level = self.reader.get_finest_level()
        
        existing_tiles = list(self.reader.iter_non_empty_tiles(level))
        
        if not existing_tiles:
            raise ValueError("No tiles found in file!")
        
        num_samples = min(max_samples, len(existing_tiles))
        step = len(existing_tiles) / num_samples
        sampled = [existing_tiles[int(i * step)] for i in range(num_samples)]
        
        grid_rows = (num_samples + grid_cols - 1) // grid_cols
        
        output_width = grid_cols * (tile_display_size + 1) + 1
        output_height = grid_rows * (tile_display_size + 1) + 1
        
        mosaic = Image.new('RGB', (output_width, output_height), border_color)
        
        for i, (row, col, offset, length) in enumerate(sampled):
            if progress_callback:
                progress_callback(i + 1, num_samples)
            
            tile_img = self.reader.read_tile_as_image(row, col, level)
            
            if tile_img is not None:
                if tile_img.mode != 'RGB':
                    tile_img = tile_img.convert('RGB')
                tile_img = tile_img.resize(
                    (tile_display_size, tile_display_size),
                    Image.Resampling.LANCZOS
                )
                
                # Add debug overlay
                tile_img = self._draw_debug_label(tile_img, row, col)
                
                # Grid position (NOT spatial position)
                grid_row = i // grid_cols
                grid_col = i % grid_cols
                
                x = grid_col * (tile_display_size + 1) + 1
                y = grid_row * (tile_display_size + 1) + 1
                
                mosaic.paste(tile_img, (x, y))
        
        return mosaic
    
    def create_coverage_map(
        self,
        level: Optional[LevelInfo] = None,
        scale: int = 1,
        empty_color: Tuple[int, int, int] = (32, 32, 32),
        filled_color: Tuple[int, int, int] = (0, 255, 0),
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> 'Image.Image':
        """Create a coverage map showing which tiles exist."""
        if not HAS_PIL:
            raise RuntimeError("PIL not installed")
        
        if level is None:
            level = self.reader.get_finest_level()
        
        width = level.grid_cols * scale
        height = level.grid_rows * scale
        
        img = Image.new('RGB', (width, height), empty_color)
        pixels = img.load()
        
        total = level.grid_cols * level.grid_rows
        
        self.reader.file.seek(level.index_offset)
        index_data = self.reader.file.read(level.index_length)
        
        for row in range(level.grid_rows):
            for col in range(level.grid_cols):
                current = row * level.grid_cols + col
                if progress_callback and current % 50000 == 0:
                    progress_callback(current, total)
                
                entry_idx = row * level.grid_cols + col
                entry_offset = entry_idx * INDEX_ENTRY_SIZE
                length = int.from_bytes(
                    index_data[entry_offset + 5:entry_offset + 8], 'little'
                )
                
                if length > 0:
                    for dy in range(scale):
                        for dx in range(scale):
                            px = col * scale + dx
                            py = row * scale + dy
                            if px < width and py < height:
                                pixels[px, py] = filled_color
        
        if progress_callback:
            progress_callback(total, total)
        
        return img


# ============================================================================
# CLI Commands
# ============================================================================

def cmd_info(args):
    """Show file information including coverage analysis."""
    with SwtilesReader(args.input) as reader:
        h = reader.header
        
        print(f"\n{'='*60}")
        print(f"  SWTILES File Information")
        print(f"{'='*60}")
        
        file_size = args.input.stat().st_size
        if file_size > 1024 * 1024 * 1024:
            size_str = f"{file_size / 1024 / 1024 / 1024:.2f} GB"
        else:
            size_str = f"{file_size / 1024 / 1024:.2f} MB"
        
        print(f"\n  File: {args.input}")
        print(f"  Size: {size_str}")
        
        print(f"\n  Header:")
        print(f"    Version:      {h.version}")
        print(f"    Data Type:    {h.data_type.name}")
        print(f"    Image Format: {h.image_format.name}")
        print(f"    CRS:          EPSG:{h.crs_epsg}")
        print(f"    Tile Size:    {h.tile_size_px}×{h.tile_size_px} px")
        print(f"    Num Levels:   {h.num_levels}")
        
        print(f"\n  Declared Bounds:")
        print(f"    E: {h.bounds_min_e:,.0f} → {h.bounds_max_e:,.0f}")
        print(f"    N: {h.bounds_min_n:,.0f} → {h.bounds_max_n:,.0f}")
        
        for level in reader.levels:
            print(f"\n  Level {level.level_id}:")
            print(f"    Resolution:   {level.resolution_m} m/px")
            print(f"    Tile Extent:  {level.tile_extent_m} m")
            print(f"    Grid:         {level.grid_cols} × {level.grid_rows}")
            print(f"    Declared:     {level.tile_count:,} tiles")
            print(f"    Origin:       ({level.origin_e:,.0f}, {level.origin_n:,.0f})")
            
            print(f"\n    Scanning actual coverage...")
            
            bar = ProgressBar("Scanning")
            coverage = reader.get_tile_coverage(level, progress_callback=bar.update)
            bar.finish()
            
            if coverage.non_empty_count == 0:
                print(f"    ⚠ NO TILES FOUND IN FILE!")
            else:
                print(f"    Actual tiles: {coverage.non_empty_count:,}")
                print(f"    Data size:    {coverage.total_size / 1024 / 1024:.2f} MB")
                print(f"    Row range:    {coverage.row_range[0]} → {coverage.row_range[1]}")
                print(f"    Col range:    {coverage.col_range[0]} → {coverage.col_range[1]}")
                print(f"    Grid extent:  {coverage.grid_extent[0]} × {coverage.grid_extent[1]} tiles")
                
                # Show first few tile positions
                print(f"\n    First 10 tile positions (row, col):")
                for i, (row, col) in enumerate(coverage.tile_positions[:10]):
                    bounds = reader.rowcol_to_bounds(row, col, level)
                    print(f"      [{i}] row={row}, col={col} → "
                          f"E({bounds[0]:.0f}-{bounds[2]:.0f}) N({bounds[1]:.0f}-{bounds[3]:.0f})")
                
                if len(coverage.tile_positions) > 10:
                    print(f"      ... and {len(coverage.tile_positions) - 10} more")
                
                b = coverage.bounds
                print(f"\n    Actual Bounds (SWEREF 99):")
                print(f"      E: {b[0]:,.0f} → {b[2]:,.0f}")
                print(f"      N: {b[1]:,.0f} → {b[3]:,.0f}")
                print(f"      Width:  {(b[2] - b[0]) / 1000:.1f} km")
                print(f"      Height: {(b[3] - b[1]) / 1000:.1f} km")
    
    return 0


def cmd_mosaic(args):
    """Create spatially correct mosaic."""
    if not HAS_PIL:
        print("  ✗ PIL not installed. Run: pip install Pillow")
        return 1
    
    with SwtilesReader(args.input) as reader:
        level = reader.get_finest_level()
        
        print(f"\n  Scanning coverage...")
        bar = ProgressBar("Scanning")
        coverage = reader.get_tile_coverage(level, progress_callback=bar.update)
        bar.finish()
        
        if coverage.non_empty_count == 0:
            print(f"  ✗ No tiles in file!")
            return 1
        
        print(f"\n  File contains {coverage.non_empty_count:,} tiles")
        print(f"  Row range: {coverage.row_range[0]} → {coverage.row_range[1]}")
        print(f"  Col range: {coverage.col_range[0]} → {coverage.col_range[1]}")
        print(f"  Grid extent: {coverage.grid_extent[0]} × {coverage.grid_extent[1]}")
        
        if coverage.non_empty_count > args.max_tiles:
            print(f"  ✗ Too many tiles ({coverage.non_empty_count}). "
                  f"Use --max-tiles to increase limit.")
            return 1
        
        generator = MosaicGenerator(reader, debug=args.debug)
        
        print(f"\n  Creating spatial mosaic...")
        print(f"  Scale: {args.scale}x")
        print(f"  Debug mode: {args.debug}")
        
        bar = ProgressBar("Reading tiles")
        mosaic, debug_info = generator.create_spatial_mosaic(
            level=level,
            coverage=coverage,
            max_tiles=args.max_tiles,
            scale=args.scale,
            progress_callback=bar.update
        )
        bar.finish()
        
        output = args.output or "mosaic.png"
        mosaic.save(output)
        print(f"\n  ✓ Saved to: {output} ({mosaic.width}×{mosaic.height} px)")
        
        return 0


def cmd_overview(args):
    """Create sampled overview grid (not spatial)."""
    if not HAS_PIL:
        print("  ✗ PIL not installed. Run: pip install Pillow")
        return 1
    
    with SwtilesReader(args.input) as reader:
        level = reader.get_finest_level()
        
        print(f"\n  Scanning for existing tiles...")
        bar = ProgressBar("Scanning")
        coverage = reader.get_tile_coverage(level, progress_callback=bar.update)
        bar.finish()
        
        if coverage.non_empty_count == 0:
            print(f"  ✗ No tiles found in file!")
            return 1
        
        print(f"  Found {coverage.non_empty_count:,} tiles")
        
        grid_cols, grid_rows = args.grid
        max_samples = grid_cols * grid_rows
        
        print(f"\n  Creating overview grid (NOT spatial - for quick inspection)")
        print(f"  Sample grid: {grid_cols} × {grid_rows}")
        print(f"  Will sample: {min(max_samples, coverage.non_empty_count)} tiles")
        
        generator = MosaicGenerator(reader, debug=args.debug)
        
        bar = ProgressBar("Reading tiles")
        mosaic = generator.create_overview_grid(
            max_samples=max_samples,
            level=level,
            tile_display_size=args.thumb_size,
            grid_cols=grid_cols,
            progress_callback=bar.update
        )
        bar.finish()
        
        output = args.output or "overview.png"
        mosaic.save(output)
        print(f"\n  ✓ Saved to: {output} ({mosaic.width}×{mosaic.height} px)")
        print(f"\n  Note: This is a sample grid, not a geographic mosaic.")
        print(f"  Use 'mosaic' command for spatially correct output.")
        
        return 0


def cmd_coverage(args):
    """Generate coverage map."""
    if not HAS_PIL:
        print("  ✗ PIL not installed. Run: pip install Pillow")
        return 1
    
    with SwtilesReader(args.input) as reader:
        level = reader.get_finest_level()
        
        print(f"\n  Generating coverage map...")
        print(f"  Grid: {level.grid_cols} × {level.grid_rows}")
        print(f"  Scale: {args.scale}x")
        
        generator = MosaicGenerator(reader)
        
        bar = ProgressBar("Scanning index")
        coverage_map = generator.create_coverage_map(
            level=level,
            scale=args.scale,
            progress_callback=bar.update
        )
        bar.finish()
        
        output = args.output or "coverage.png"
        coverage_map.save(output)
        print(f"  ✓ Saved to: {output} ({coverage_map.width}×{coverage_map.height} px)")
        
        return 0


def cmd_tile(args):
    """Extract single tile."""
    with SwtilesReader(args.input) as reader:
        level = reader.get_finest_level()
        
        print(f"\n  Scanning coverage...")
        bar = ProgressBar("Scanning")
        coverage = reader.get_tile_coverage(level, progress_callback=bar.update)
        bar.finish()
        
        if coverage.non_empty_count == 0:
            print(f"  ✗ No tiles in file!")
            return 1
        
        print(f"\n  File has {coverage.non_empty_count:,} tiles")
        print(f"  Row range: {coverage.row_range[0]} - {coverage.row_range[1]}")
        print(f"  Col range: {coverage.col_range[0]} - {coverage.col_range[1]}")
        
        if args.coord:
            easting, northing = args.coord
            row, col = reader.coord_to_rowcol(easting, northing, level)
            print(f"\n  Coordinate ({easting}, {northing}) → row={row}, col={col}")
        elif args.row is not None and args.col is not None:
            row, col = args.row, args.col
        else:
            row, col = coverage.tile_positions[0]
            print(f"\n  No row/col specified, using first tile at row={row}, col={col}")
        
        print(f"  Reading tile at row={row}, col={col}")
        
        tile_data = reader.read_tile(row, col, level)
        
        if tile_data is None:
            print(f"  ✗ Tile is empty at row={row}, col={col}")
            return 1
        
        bounds = reader.rowcol_to_bounds(row, col, level)
        print(f"  Bounds: E({bounds[0]:.0f}-{bounds[2]:.0f}) "
              f"N({bounds[1]:.0f}-{bounds[3]:.0f})")
        print(f"  Size: {len(tile_data):,} bytes")
        
        ext = reader.header.image_format.name.lower()
        output = args.output or f"tile_{row}_{col}.{ext}"
        Path(output).write_bytes(tile_data)
        print(f"  ✓ Saved to: {output}")
        
        return 0


def cmd_debug(args):
    """Debug tile positions and file structure."""
    with SwtilesReader(args.input) as reader:
        h = reader.header
        level = reader.get_finest_level()
        
        print(f"\n{'='*60}")
        print(f"  SWTILES Debug Report")
        print(f"{'='*60}")
        
        print(f"\n  File structure:")
        print(f"    Header: bytes 0-{HEADER_SIZE-1}")
        print(f"    Level table: bytes {h.level_table_offset}-{h.level_table_offset + h.num_levels * LEVEL_ENTRY_SIZE - 1}")
        print(f"    Level 0 index: bytes {level.index_offset}-{level.index_offset + level.index_length - 1}")
        print(f"    Level 0 data: bytes {level.data_offset}+")
        
        print(f"\n  Grid parameters:")
        print(f"    Origin: ({level.origin_e}, {level.origin_n})")
        print(f"    Tile extent: {level.tile_extent_m} m")
        print(f"    Grid size: {level.grid_cols} cols × {level.grid_rows} rows")
        
        print(f"\n  Coordinate mapping formula:")
        print(f"    col = (easting - {level.origin_e}) / {level.tile_extent_m}")
        print(f"    row = ({level.origin_n} - northing) / {level.tile_extent_m}")
        
        print(f"\n  Pixel position formula:")
        print(f"    x = (col - min_col) * tile_size")
        print(f"    y = (row - min_row) * tile_size")
        
        # Scan and show tiles
        print(f"\n  Scanning tiles...")
        bar = ProgressBar("Scanning")
        coverage = reader.get_tile_coverage(level, progress_callback=bar.update)
        bar.finish()
        
        if coverage.non_empty_count == 0:
            print(f"\n  ✗ No tiles found!")
            return 1
        
        print(f"\n  Coverage summary:")
        print(f"    Tiles: {coverage.non_empty_count}")
        print(f"    Row range: {coverage.row_range[0]} → {coverage.row_range[1]}")
        print(f"    Col range: {coverage.col_range[0]} → {coverage.col_range[1]}")
        
        min_row, max_row = coverage.row_range
        min_col, max_col = coverage.col_range
        
        print(f"\n  Expected mosaic layout:")
        print(f"    Top-left tile: row={min_row}, col={min_col} → pixel (0, 0)")
        print(f"    Bottom-right tile: row={max_row}, col={max_col} → "
              f"pixel ({(max_col-min_col)*h.tile_size_px}, {(max_row-min_row)*h.tile_size_px})")
        
        print(f"\n  First 20 tiles with expected positions:")
        tile_size = h.tile_size_px
        for i, (row, col) in enumerate(coverage.tile_positions[:20]):
            x = (col - min_col) * tile_size
            y = (row - min_row) * tile_size
            bounds = reader.rowcol_to_bounds(row, col, level)
            print(f"    [{i:2d}] row={row:4d} col={col:4d} → pixel ({x:5d}, {y:5d}) "
                  f"| E={bounds[0]:.0f} N={bounds[3]:.0f}")
        
        # Check for gaps
        print(f"\n  Checking for contiguity...")
        tile_set = set(coverage.tile_positions)
        expected = (max_row - min_row + 1) * (max_col - min_col + 1)
        actual = len(tile_set)
        
        if actual == expected:
            print(f"    ✓ Perfect coverage: {actual} tiles fill the bounding box")
        else:
            missing = expected - actual
            print(f"    ⚠ Sparse coverage: {actual}/{expected} cells filled ({missing} gaps)")
            
            # Show some gaps
            gaps = []
            for r in range(min_row, min(min_row + 5, max_row + 1)):
                for c in range(min_col, min(min_col + 10, max_col + 1)):
                    if (r, c) not in tile_set:
                        gaps.append((r, c))
            
            if gaps:
                print(f"    First gaps in top-left region:")
                for r, c in gaps[:10]:
                    print(f"      Missing: row={r}, col={c}")
        
        return 0


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='SWTILES Reader and Mosaic Generator v2.2',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Show file info
  python swtiles_reader.py info raster.swtiles

  # Debug tile positions
  python swtiles_reader.py debug raster.swtiles

  # Create spatially correct mosaic
  python swtiles_reader.py mosaic raster.swtiles -o mosaic.png

  # Create mosaic with debug labels
  python swtiles_reader.py mosaic raster.swtiles -o mosaic.png --debug

  # Create mosaic at 50% scale
  python swtiles_reader.py mosaic raster.swtiles -o mosaic.png --scale 0.5

  # Create overview grid (quick inspection, not spatial)
  python swtiles_reader.py overview raster.swtiles -o overview.png
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', required=True)
    
    # Info command
    p_info = subparsers.add_parser('info', help='Show file information')
    p_info.add_argument('input', type=Path, help='Input SWTILES file')
    
    # Debug command
    p_debug = subparsers.add_parser('debug', help='Debug tile positions')
    p_debug.add_argument('input', type=Path, help='Input SWTILES file')
    
    # Coverage command
    p_coverage = subparsers.add_parser('coverage', help='Generate coverage map')
    p_coverage.add_argument('input', type=Path, help='Input SWTILES file')
    p_coverage.add_argument('--scale', type=int, default=1,
                            help='Pixels per tile (default: 1)')
    p_coverage.add_argument('-o', '--output', help='Output filename')
    
    # Tile command
    p_tile = subparsers.add_parser('tile', help='Extract single tile')
    p_tile.add_argument('input', type=Path, help='Input SWTILES file')
    p_tile.add_argument('--row', type=int, help='Tile row')
    p_tile.add_argument('--col', type=int, help='Tile column')
    p_tile.add_argument('--coord', type=float, nargs=2, metavar=('E', 'N'),
                        help='Coordinate (easting northing)')
    p_tile.add_argument('-o', '--output', help='Output filename')
    
    # Mosaic command
    p_mosaic = subparsers.add_parser('mosaic', help='Create spatially correct mosaic')
    p_mosaic.add_argument('input', type=Path, help='Input SWTILES file')
    p_mosaic.add_argument('--max-tiles', type=int, default=10000,
                          help='Maximum tiles (default: 10000)')
    p_mosaic.add_argument('--scale', type=float, default=1.0,
                          help='Scale factor (default: 1.0)')
    p_mosaic.add_argument('--debug', action='store_true',
                          help='Draw row/col labels on tiles')
    p_mosaic.add_argument('-o', '--output', help='Output filename')
    
    # Overview command
    p_overview = subparsers.add_parser('overview',
                                        help='Create sampled overview grid')
    p_overview.add_argument('input', type=Path, help='Input SWTILES file')
    p_overview.add_argument('--grid', type=int, nargs=2, default=[20, 20],
                            metavar=('COLS', 'ROWS'), help='Grid size')
    p_overview.add_argument('--thumb-size', type=int, default=100,
                            help='Thumbnail size (default: 100)')
    p_overview.add_argument('--debug', action='store_true',
                            help='Draw row/col labels on tiles')
    p_overview.add_argument('-o', '--output', help='Output filename')
    
    args = parser.parse_args()
    
    print(f"\n{'='*60}")
    print(f"  SWTILES Reader v2.2")
    print(f"{'='*60}")
    
    commands = {
        'info': cmd_info,
        'debug': cmd_debug,
        'coverage': cmd_coverage,
        'tile': cmd_tile,
        'mosaic': cmd_mosaic,
        'overview': cmd_overview,
    }
    
    try:
        return commands[args.command](args)
    except FileNotFoundError as e:
        print(f"\n  ✗ Error: {e}")
        return 1
    except ValueError as e:
        print(f"\n  ✗ Error: {e}")
        return 1
    except Exception as e:
        print(f"\n  ✗ Unexpected error: {e}")
        raise


if __name__ == '__main__':
    sys.exit(main() or 0)