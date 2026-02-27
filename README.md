# SWTILES: Optimized Swedish Map Tile Archive

**SWTILES** is a high-performance, single-file tile archive format designed for serving Swedish national map data (orthophotos and terrain) directly from cloud storage via HTTP range requests.

## 🚀 The Goal
Traditional tile servers are expensive to maintain. SWTILES allows you to host gigabytes of Swedish geographic data on affordable S3-compatible storage (like Cloudflare R2) and fetch individual tiles (500x500px) directly in the browser using a thin JavaScript client.

## ✨ Key Features
- **Arbitrary CRS Support**: Native support for **SWEREF 99 TM (EPSG:3006)**, avoiding the distortion of Web Mercator.
- **Cloud Native**: Optimized for **HTTP Range Requests**—fetch only the bytes you need.
- **Single-File Archive**: Manage one `.swtiles` file instead of millions of small image files.
- **O(1) Lookup**: Flat binary index for instant tile retrieval.
- **3D Ready**: Includes a Three.js viewer that drapes orthophotos over decoded Terrain-RGB meshes.

---

## 🏗️ Technical Architecture

### The Format (v2.0)
The `.swtiles` format is a custom binary structure consisting of:
1.  **Header (256 bytes)**: Global metadata (CRS, bounds, data type).
2.  **Level Table**: Metadata for different resolution levels.
3.  **Spatial Index**: A flat array of 8-byte entries (offset/length) for every grid cell.
4.  **Tile Data**: Concatenated WebP (raster) or PNG (terrain) blobs.

### The Stack
- **Python**: Data acquisition, slicing, and archive generation.
- **Cloudflare R2**: Cost-effective hosting for large archives (30GB+).
- **Cloudflare Workers**: An access-control proxy to handle CORS and origin-based filtering.
- **Vanilla JS**: A client-side reader that parses binary data in the browser.

---

## 🛠️ Data Pipeline
Converting raw Lantmäteriet data into a functional map viewer follows this workflow:

1.  **Acquisition**: `download_and_convert_sweden.py` fetches and converts raw API data.
2.  **Tiling**: `resize_tiles.py` slices large 50km images into web-ready 500px chunks.
3.  **Archiving**: `swtile_writer.py` packs the chunks into the `.swtiles` format.
4.  **Verification**: `swtiles_reader.py` verifies spatial alignment and data integrity.

For a detailed step-by-step walkthrough, see the [**PIPELINE_GUIDE.md**](./PIPELINE_GUIDE.md).

---

## 🖥️ Web Viewing
The project includes two reference viewers in the `webapp/` folder:
- **2D Viewer**: A canvas-based navigator for panning and zooming across Sweden.
- **3D Viewer**: A WebGL engine (Three.js) that visualizes the Swedish landscape in 3D using Terrain-RGB decoding.

---

## ⚖️ License
This project is provided as-is for processing Swedish open data. The map data itself from Lantmäteriet is typically available under CC0 or open data licenses (verify with the source).

*Created by [Tobias Cornvik](https://github.com/DrToblo)*
