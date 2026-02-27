/**
 * SWTILES Reader - Browser library for reading SWTILES archives via HTTP range requests
 */

const HEADER_SIZE = 256;
const LEVEL_ENTRY_SIZE = 64;
const INDEX_ENTRY_SIZE = 8;

const IMAGE_FORMATS = {
  1: 'image/webp',
  2: 'image/png',
  3: 'image/jpeg',
  4: 'image/avif',
};

const DATA_TYPES = {
  1: 'raster',
  2: 'terrain',
  3: 'other',
};

export class SwtilesReader {
  constructor(url) {
    this.url = url;
    this.header = null;
    this.levels = null;
    this._initialized = false;
  }

  /**
   * Fetch a byte range from the file
   */
  async fetchRange(start, length) {
    const response = await fetch(this.url, {
      headers: {
        'Range': `bytes=${start}-${start + length - 1}`,
      },
    });

    if (!response.ok && response.status !== 206) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    return response.arrayBuffer();
  }

  /**
   * Parse the 256-byte file header
   */
  parseHeader(buffer) {
    const view = new DataView(buffer);
    const decoder = new TextDecoder();

    // Verify magic
    const magic = decoder.decode(new Uint8Array(buffer, 0, 8));
    if (magic !== 'SWTILES\0') {
      throw new Error(`Invalid magic: expected "SWTILES\\0", got "${magic}"`);
    }

    return {
      version: view.getUint16(8, true),
      dataType: view.getUint8(10),
      dataTypeName: DATA_TYPES[view.getUint8(10)] || 'unknown',
      imageFormat: view.getUint8(11),
      mimeType: IMAGE_FORMATS[view.getUint8(11)] || 'application/octet-stream',
      crsEpsg: view.getUint32(12, true),
      bounds: {
        minE: view.getFloat64(16, true),
        minN: view.getFloat64(24, true),
        maxE: view.getFloat64(32, true),
        maxN: view.getFloat64(40, true),
      },
      tileSizePx: view.getUint16(48, true),
      numLevels: view.getUint8(50),
      levelTableOffset: view.getBigUint64(52, true),
    };
  }

  /**
   * Parse a 64-byte level table entry
   */
  parseLevelEntry(buffer, offset) {
    const view = new DataView(buffer, offset, LEVEL_ENTRY_SIZE);

    return {
      levelId: view.getUint8(0),
      resolutionM: view.getFloat32(2, true),
      tileExtentM: view.getFloat32(6, true),
      originE: view.getFloat64(12, true),
      originN: view.getFloat64(20, true),
      gridCols: view.getUint32(28, true),
      gridRows: view.getUint32(32, true),
      tileCount: view.getUint32(36, true),
      indexOffset: view.getBigUint64(40, true),
      indexLength: view.getBigUint64(48, true),
      dataOffset: view.getBigUint64(56, true),
    };
  }

  /**
   * Parse an 8-byte index entry (5-byte offset + 3-byte length)
   */
  parseIndexEntry(buffer) {
    const bytes = new Uint8Array(buffer);

    // Read 5-byte little-endian offset (uint40)
    const offset =
      BigInt(bytes[0]) |
      (BigInt(bytes[1]) << 8n) |
      (BigInt(bytes[2]) << 16n) |
      (BigInt(bytes[3]) << 24n) |
      (BigInt(bytes[4]) << 32n);

    // Read 3-byte little-endian length (uint24)
    const length = bytes[5] | (bytes[6] << 8) | (bytes[7] << 16);

    return { offset, length };
  }

  /**
   * Initialize reader by fetching header and level table
   */
  async init() {
    if (this._initialized) return;

    // Fetch header
    const headerBuffer = await this.fetchRange(0, HEADER_SIZE);
    this.header = this.parseHeader(headerBuffer);

    if (this.header.version !== 2) {
      throw new Error(`Unsupported version: ${this.header.version}`);
    }

    // Fetch level table
    const levelTableSize = this.header.numLevels * LEVEL_ENTRY_SIZE;
    const levelTableBuffer = await this.fetchRange(
      Number(this.header.levelTableOffset),
      levelTableSize
    );

    this.levels = [];
    for (let i = 0; i < this.header.numLevels; i++) {
      this.levels.push(this.parseLevelEntry(levelTableBuffer, i * LEVEL_ENTRY_SIZE));
    }

    this._initialized = true;
  }

  /**
   * Get level by ID
   */
  getLevel(levelId = 0) {
    if (!this._initialized) {
      throw new Error('Reader not initialized. Call init() first.');
    }
    return this.levels.find((l) => l.levelId === levelId);
  }

  /**
   * Convert coordinates to row/col
   */
  coordToRowCol(level, easting, northing) {
    const col = Math.floor((easting - level.originE) / level.tileExtentM);
    const row = Math.floor((level.originN - northing) / level.tileExtentM);
    return { row, col };
  }

  /**
   * Convert row/col to tile bounds
   */
  rowColToBounds(level, row, col) {
    const minE = level.originE + col * level.tileExtentM;
    const maxE = minE + level.tileExtentM;
    const maxN = level.originN - row * level.tileExtentM;
    const minN = maxN - level.tileExtentM;
    return { minE, minN, maxE, maxN };
  }

  /**
   * Check if row/col is within grid bounds
   */
  isValidRowCol(level, row, col) {
    return row >= 0 && row < level.gridRows && col >= 0 && col < level.gridCols;
  }

  /**
   * Fetch a tile by row/col
   * Returns { blob, bounds } or null if tile is empty
   */
  async getTile(levelId, row, col) {
    if (!this._initialized) {
      await this.init();
    }

    const level = this.getLevel(levelId);
    if (!level) {
      throw new Error(`Level ${levelId} not found`);
    }

    if (!this.isValidRowCol(level, row, col)) {
      return null;
    }

    // Calculate index entry position
    const indexEntryOffset =
      Number(level.indexOffset) + (row * level.gridCols + col) * INDEX_ENTRY_SIZE;

    // Fetch index entry
    const indexBuffer = await this.fetchRange(indexEntryOffset, INDEX_ENTRY_SIZE);
    const { offset: tileOffset, length: tileLength } = this.parseIndexEntry(indexBuffer);

    // Empty tile
    if (tileLength === 0) {
      return null;
    }

    // Fetch tile data
    const absoluteOffset = Number(level.dataOffset) + Number(tileOffset);
    const tileBuffer = await this.fetchRange(absoluteOffset, tileLength);

    const blob = new Blob([tileBuffer], { type: this.header.mimeType });
    const bounds = this.rowColToBounds(level, row, col);

    return { blob, bounds, row, col };
  }

  /**
   * Fetch a tile by coordinate
   */
  async getTileByCoord(levelId, easting, northing) {
    if (!this._initialized) {
      await this.init();
    }

    const level = this.getLevel(levelId);
    if (!level) {
      throw new Error(`Level ${levelId} not found`);
    }

    const { row, col } = this.coordToRowCol(level, easting, northing);
    return this.getTile(levelId, row, col);
  }

  /**
   * Get tiles for a viewport (returns array of {row, col, bounds})
   */
  getTilesInView(levelId, viewMinE, viewMinN, viewMaxE, viewMaxN) {
    const level = this.getLevel(levelId);
    if (!level) return [];

    const { row: startRow, col: startCol } = this.coordToRowCol(level, viewMinE, viewMaxN);
    const { row: endRow, col: endCol } = this.coordToRowCol(level, viewMaxE, viewMinN);

    const tiles = [];
    for (let row = Math.max(0, startRow); row <= Math.min(level.gridRows - 1, endRow); row++) {
      for (let col = Math.max(0, startCol); col <= Math.min(level.gridCols - 1, endCol); col++) {
        tiles.push({
          row,
          col,
          bounds: this.rowColToBounds(level, row, col),
        });
      }
    }

    return tiles;
  }
}
