# SWTILES Data Processing Pipeline Guide

This guide outlines the complete workflow for converting Swedish national map data (Lantmäteriet) into the optimized `.swtiles` format for high-performance web viewing via HTTP range requests.

---

## 1. Data Acquisition
**Script:** `download_and_convert_sweden.py`

This script fetches raw elevation data (NH) and converts it into **Terrain-RGB** format (elevation packed into RGB channels for GPU decoding).

*   **Prerequisites:** A `.env` file in the root directory containing your Lantmäteriet API credentials:
    ```env
    LM_CONSUMER_KEY=your_key_here
    LM_CONSUMER_SECRET=your_secret_here
    ```
*   **Execution:**
    ```bash
    python download_and_convert_sweden.py
    ```
*   **Output:** Creates a directory `Karta_Hojd_Sverige/` containing:
    *   Subdirectories with 50km × 50km PNG files.
    *   `mosaik_hojd.vrt`: A GDAL manifest of all downloaded files.
*   **Next Step Preparation:** Rename `mosaik_hojd.vrt` to `mosaik.vrt` inside the `Karta_Hojd_Sverige/` folder.

---

## 2. Tiling (Resizing)
**Script:** `resize_tiles.py`

Browser-based viewers cannot handle 50km images. This script "explodes" the master images into small, web-ready chunks.

*   **Input Requirement:** A folder named exactly like the dataset (e.g., `Karta_Hojd_Sverige/`) containing a `mosaik.vrt` file.
*   **Execution:**
    ```bash
    python resize_tiles.py
    ```
*   **Output:** Creates a new directory `Karta_Hojd_Sverige_tiled/` containing:
    *   Thousands of **500px × 500px** PNG/WebP tiles.
    *   A new `mosaik.vrt` that references these smaller tiles.

---

## 3. Archiving
**Script:** `swtile_writer.py`

This step packs all individual files into a single binary archive optimized for HTTP Range Requests, creating a spatial index for O(1) lookup.

*   **Execution (Terrain):**
    ```bash
    python swtile_writer.py Karta_Hojd_Sverige_tiled/mosaik.vrt terrain.swtiles --data-type terrain
    ```
*   **Execution (Orthophoto):**
    ```bash
    python swtile_writer.py Karta_10000_webp_tiled/mosaik.vrt ortho.swtiles --data-type raster
    ```
*   **Options:** 
    *   `--test 100`: Create a small 100-tile archive for quick testing.
    *   `--dry-run`: Validate VRT and source files without writing.

---

## 4. Verification
**Script:** `swtiles_reader.py`

Before uploading gigabytes of data to the cloud, verify the archive locally.

*   **Check Metadata & Coverage:**
    ```bash
    python swtiles_reader.py info terrain.swtiles
    ```
*   **Visual Verify (Mosaic):**
    ```bash
    # Creates a 10% scale image of the entire archive with coordinate labels
    python swtiles_reader.py mosaic terrain.swtiles -o verify.png --scale 0.1 --debug
    ```
*   **Extract Single Tile:**
    ```bash
    python swtiles_reader.py tile terrain.swtiles --coord 580000 7240000 -o sample.png
    ```

---

## 5. Cloud Deployment & Frontend Viewing

### Storage & Access
1.  **Storage:** Upload the resulting `.swtiles` files to a **Cloudflare R2** bucket.
2.  **Access Control:** Deploy the Cloudflare Worker in `worker/` to provide a CORS-enabled proxy to your R2 bucket.

### Frontend Integration
Use the `webapp/swtiles.js` library to consume the data in your application:

```javascript
import { SwtilesReader } from './swtiles.js';

const reader = new SwtilesReader("https://your-worker-url.com/data.swtiles");
await reader.initialize();

// Get tile by coordinates (SWEREF 99 TM)
const tile = await reader.getTileAtCoord(580000, 7240000);
```

### Viewers
*   **`webapp/index.html`**: 2D canvas-based navigator.
*   **`webapp/viewer3d.html`**: Three.js 3D engine draping orthophotos over elevation meshes.
