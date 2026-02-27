import os
import xml.etree.ElementTree as ET
from PIL import Image
from concurrent.futures import ProcessPoolExecutor, as_completed
from tqdm import tqdm

# --- CONFIGURATION ---
TEST_MODE = False  # Set to False to process the full catalog
TEST_LIMIT = 10   # Number of files to process in test mode
MAX_WORKERS = os.cpu_count()  # Adjust if you want to limit CPU usage

DATASETS = [
    {
        "name": "Karta_10000_webp",
        "ext": ".webp",
        "slice_size": 500,
        "save_kwargs": {"quality": 80},
    },
    {
        "name": "Karta_Hojd_Sverige",
        "ext": ".png",
        "slice_size": 500,
        "save_kwargs": {"compress_level": 6},
    }
]

def ensure_dir(path):
    if not os.path.exists(path):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass # Handle race conditions in multiprocessing

def process_single_source_file(source_data):
    """
    Worker function to process a single original image.
    Returns a list of dictionaries containing info to build the new VRT entries.
    """
    config, src_rel_path, dst_rect_data, root_dir, output_dir = source_data
    
    # Paths
    full_src_path = os.path.join(root_dir, src_rel_path.replace("\\", "/"))
    base_name = os.path.splitext(os.path.basename(src_rel_path))[0]
    sub_folder = os.path.dirname(src_rel_path)
    
    # Global Offsets from original VRT
    global_x_start = float(dst_rect_data['xOff'])
    global_y_start = float(dst_rect_data['yOff'])
    
    vrt_entries = []
    
    try:
        if not os.path.exists(full_src_path):
            return []

        with Image.open(full_src_path) as img:
            width, height = img.size
            
            # Iterate through the image in 500x500 chunks
            for y in range(0, height, config["slice_size"]):
                for x in range(0, width, config["slice_size"]):
                    
                    # Define new filename: OriginalName_Yoffset_Xoffset.ext
                    # Example: 646_52_00_0_500.webp
                    new_filename = f"{base_name}_{y}_{x}{config['ext']}"
                    new_rel_path = os.path.join(sub_folder, new_filename)
                    full_out_path = os.path.join(output_dir, new_rel_path)
                    
                    # RESUME LOGIC: Skip if file exists
                    if not os.path.exists(full_out_path):
                        box = (x, y, x + config["slice_size"], y + config["slice_size"])
                        tile = img.crop(box)
                        ensure_dir(os.path.dirname(full_out_path))
                        tile.save(full_out_path, **config["save_kwargs"])
                    
                    # Prepare VRT metadata for this tile
                    vrt_entries.append({
                        "filename": new_rel_path,
                        "src_x": 0, "src_y": 0,
                        "src_w": config["slice_size"], "src_h": config["slice_size"],
                        "dst_x": global_x_start + x,
                        "dst_y": global_y_start + y,
                        "dst_w": config["slice_size"], "dst_h": config["slice_size"]
                    })
                    
    except Exception as e:
        print(f"\nError processing {full_src_path}: {e}")
        return []

    return vrt_entries

def process_dataset(config):
    root_dir = config["name"]
    vrt_path = os.path.join(root_dir, "mosaik.vrt")
    output_dir = f"{root_dir}_tiled"
    new_vrt_path = os.path.join(output_dir, "mosaik.vrt")
    
    if not os.path.exists(vrt_path):
        print(f"Skipping {root_dir}: mosaik.vrt not found.")
        return

    print(f"\n--- Processing {root_dir} ---")
    ensure_dir(output_dir)

    # 1. Parse Original VRT to get list of files
    tree = ET.parse(vrt_path)
    root = tree.getroot()
    orig_band = root.find("VRTRasterBand")
    sources = orig_band.findall("SimpleSource")

    # 2. Filter for Test Mode
    if TEST_MODE:
        print(f"TEST MODE: Processing only {TEST_LIMIT} of {len(sources)} files.")
        sources = sources[:TEST_LIMIT]

    # 3. Prepare Tasks
    tasks = []
    for source in sources:
        src_filename = source.find("SourceFilename").text
        dst_rect = source.find("DstRect").attrib
        tasks.append((config, src_filename, dst_rect, root_dir, output_dir))

    # 4. Initialize New VRT Structure (Header)
    new_root = ET.Element("VRTDataset")
    new_root.set("rasterXSize", root.get("rasterXSize"))
    new_root.set("rasterYSize", root.get("rasterYSize"))
    
    for tag in ["SRS", "GeoTransform"]:
        elem = root.find(tag)
        if elem is not None:
            new_root.append(elem)
            
    new_band = ET.SubElement(new_root, "VRTRasterBand")
    new_band.set("dataType", orig_band.get("dataType"))
    new_band.set("band", "1")
    if orig_band.find("ColorInterp") is not None:
        new_band.append(orig_band.find("ColorInterp"))

    # 5. Execute Parallel Processing
    new_sources_list = []
    
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all tasks
        futures = [executor.submit(process_single_source_file, task) for task in tasks]
        
        # Process results as they finish with a progress bar
        for future in tqdm(as_completed(futures), total=len(tasks), unit="file"):
            result = future.result()
            new_sources_list.extend(result)

    # 6. Build New VRT from results
    print("Building new VRT index...")
    for entry in new_sources_list:
        sim_source = ET.SubElement(new_band, "SimpleSource")
        
        fn = ET.SubElement(sim_source, "SourceFilename")
        fn.set("relativeToVRT", "1")
        fn.text = entry["filename"]
        
        sb = ET.SubElement(sim_source, "SourceBand")
        sb.text = "1"
        
        ET.SubElement(sim_source, "SrcRect", {
            "xOff": str(entry["src_x"]), "yOff": str(entry["src_y"]),
            "xSize": str(entry["src_w"]), "ySize": str(entry["src_h"])
        })
        
        ET.SubElement(sim_source, "DstRect", {
            "xOff": str(entry["dst_x"]), "yOff": str(entry["dst_y"]),
            "xSize": str(entry["dst_w"]), "ySize": str(entry["dst_h"])
        })

    # 7. Save VRT
    tree = ET.ElementTree(new_root)
    ET.indent(tree, space="  ", level=0)
    tree.write(new_vrt_path, encoding="UTF-8", xml_declaration=False)
    print(f"Completed {root_dir}. Output in {output_dir}")

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    if TEST_MODE:
        print("!!! RUNNING IN TEST MODE (First 10 files only) !!!")
        
    for ds in DATASETS:
        process_dataset(ds)
        
    if TEST_MODE:
        print("\nTest complete. Change TEST_MODE = False in the script to run full dataset.")