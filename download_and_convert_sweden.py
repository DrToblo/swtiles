import os
import requests
import rasterio
import numpy as np
from PIL import Image
from dotenv import load_dotenv
import time
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from tqdm import tqdm
from datetime import datetime

# --- FILE LOCK SETUP ---
try:
    from filelock import FileLock
except ImportError:
    import fcntl
    class FileLock:
        def __init__(self, file_path, timeout=None):
            self.file_path = file_path
            self.fd = None
        def __enter__(self):
            self.fd = open(self.file_path, 'w')
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        def __exit__(self, exc_type, exc_val, exc_tb):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            self.fd.close()

load_dotenv()

# --- CONFIGURATION ---
CONSUMER_KEY = os.getenv("LM_CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("LM_CONSUMER_SECRET")
LM_TOKEN_URL = "https://api.lantmateriet.se/token"
LM_STAC_HOJD_URL = "https://api.lantmateriet.se/stac-hojd/v1/search"
BASE_DOWNLOAD_DIR = "Karta_Hojd_Sverige"
VRT_FILENAME = "mosaik_hojd.vrt"
STATE_FILENAME = "mosaik_state.json"
TOKEN_CACHE_FILE = "token_cache.json"
TOKEN_LOCK_FILE = "token_cache.lock"
STATE_LOCK_FILE = "mosaik_state.lock" # New lock for state file

ERROR_WAIT_TIME = 600
MAX_RETRIES = 5
TOKEN_REFRESH_INTERVAL = 3000  # 50 minutes

UA_HEADERS = {'User-Agent': 'ElevationDownloader/1.0'}

def get_runtime_config():
    current_hour = datetime.now().hour
    if 7 <= current_hour < 18:
        return 1, 0.0
    else:
        n_workers = min(4, max(1, multiprocessing.cpu_count()))
        return n_workers, 0.0

def get_session():
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    return session

def get_access_token():
    lock = FileLock(TOKEN_LOCK_FILE)
    with lock:
        if os.path.exists(TOKEN_CACHE_FILE):
            try:
                with open(TOKEN_CACHE_FILE, 'r') as f:
                    data = json.load(f)
                    if time.time() - data.get('timestamp', 0) < TOKEN_REFRESH_INTERVAL:
                        return data.get('access_token')
            except:
                pass

        if not CONSUMER_KEY or not CONSUMER_SECRET:
            print("FEL: Saknar API-nycklar i .env")
            exit(1)
        
        print(f"[{datetime.now().strftime('%H:%M')}] Hämtar ny access-token...")
        for attempt in range(MAX_RETRIES):
            try:
                session = get_session()
                response = session.post(
                    LM_TOKEN_URL,
                    auth=(CONSUMER_KEY, CONSUMER_SECRET),
                    data={"grant_type": "client_credentials"},
                    headers=UA_HEADERS,
                    timeout=20
                )
                response.raise_for_status()
                token = response.json()["access_token"]
                
                with open(TOKEN_CACHE_FILE, 'w') as f:
                    json.dump({'access_token': token, 'timestamp': time.time()}, f)
                return token
            except Exception as e:
                print(f"Token-fel (försök {attempt+1}): {e}")
                time.sleep(10)
        return None

def generate_folder_name(filename):
    try:
        parts = filename.split('_')
        if len(parts) >= 2:
            return f"{parts[0][:2]}_{parts[1][0]}"
    except:
        pass
    return "other"

def process_file(file_info, delay=0):
    # Unpack extra info passed in
    asset_href, headers, bbox = file_info
    
    filename = asset_href.split('/')[-1]
    png_filename = filename.replace('.tif', '.png')
    folder_name = generate_folder_name(filename)
    
    target_dir = os.path.join(BASE_DOWNLOAD_DIR, folder_name)
    os.makedirs(target_dir, exist_ok=True)
    
    tif_path = os.path.join(target_dir, filename)
    png_path = os.path.join(target_dir, png_filename)
    relative_path = f"{folder_name}/{png_filename}"

    # Return bbox in the result so main thread can use it
    if os.path.exists(png_path) and os.path.getsize(png_path) > 0:
        return {"status": "skipped", "path": relative_path, "bbox": bbox}

    if delay > 0:
        time.sleep(delay)
    
    current_token = get_access_token()
    if current_token:
        headers["Authorization"] = f"Bearer {current_token}"

    for attempt in range(MAX_RETRIES):
        try:
            session = get_session()
            with session.get(asset_href, headers=headers, stream=True, timeout=60) as r:
                if r.status_code == 401:
                    # Force token refresh
                    if os.path.exists(TOKEN_CACHE_FILE):
                        try: os.remove(TOKEN_CACHE_FILE)
                        except: pass
                    new_token = get_access_token()
                    if new_token:
                        headers["Authorization"] = f"Bearer {new_token}"
                        continue
                    else:
                        raise Exception("Kunde inte förnya token vid 401")

                r.raise_for_status()
                with open(tif_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=16384):
                        f.write(chunk)
            
            # Conversion
            with rasterio.open(tif_path) as src:
                height_data = src.read(1)
                nodata = src.nodata

            if nodata is not None:
                height_data[height_data == nodata] = -10000

            data_scaled = (height_data + 10000) * 10
            data_scaled = np.clip(data_scaled, 0, 16777215).astype(np.uint32)

            r_ch = (data_scaled >> 16).astype(np.uint8)
            g_ch = ((data_scaled >> 8) & 0xFF).astype(np.uint8)
            b_ch = (data_scaled & 0xFF).astype(np.uint8)

            img = Image.fromarray(np.dstack((r_ch, g_ch, b_ch)), mode='RGB')
            img.save(png_path, "PNG", optimize=False)
            
            os.remove(tif_path)
            
            # SUCCESS: Return all info needed to append to VRT immediately
            return {"status": "downloaded", "path": relative_path, "bbox": bbox}

        except Exception as e:
            if os.path.exists(tif_path): os.remove(tif_path)
            if attempt == MAX_RETRIES - 1:
                return {"status": "error", "msg": str(e), "path": relative_path}
            time.sleep(10)

    return {"status": "error", "msg": "Okänt fel", "path": relative_path}

# --- STATE MANAGEMENT ---
def load_vrt_state():
    path = os.path.join(BASE_DOWNLOAD_DIR, STATE_FILENAME)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f: return json.load(f)
        except: pass
    return {}

def save_vrt_state_safe(state):
    """Saves state atomically using FileLock."""
    path = os.path.join(BASE_DOWNLOAD_DIR, STATE_FILENAME)
    lock = FileLock(STATE_LOCK_FILE)
    with lock:
        with open(path, 'w', encoding='utf-8') as f: 
            json.dump(state, f)

def create_vrt_from_state(state, output_path):
    """Rewrites the VRT file based on current state."""
    if not state: return
    tiles = list(state.values())
    
    # Calculate global extent
    min_x = min(t['bbox'][0] for t in tiles)
    min_y = min(t['bbox'][1] for t in tiles)
    max_x = max(t['bbox'][2] for t in tiles)
    max_y = max(t['bbox'][3] for t in tiles)
    width = int(max_x - min_x)
    height = int(max_y - min_y)
    
    # Write VRT (Overwrites existing)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">\n')
        f.write('  <SRS>PROJCS["SWEREF99 TM",GEOGCS["SWEREF99",DATUM["SWEREF99",SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",15],PARAMETER["scale_factor",0.9996],PARAMETER["false_easting",500000],PARAMETER["false_northing",0],UNIT["metre",1]]</SRS>\n')
        f.write(f'  <GeoTransform>{min_x}, 1.0, 0.0, {max_y}, 0.0, -1.0</GeoTransform>\n')
        for i, color in enumerate(['Red', 'Green', 'Blue'], 1):
            f.write(f'  <VRTRasterBand dataType="Byte" band="{i}"><ColorInterp>{color}</ColorInterp>\n')
            for t in tiles:
                t_min_x, t_min_y, t_max_x, t_max_y = t['bbox']
                x_off, y_off = int(t_min_x - min_x), int(max_y - t_max_y)
                f.write(f'    <SimpleSource>\n      <SourceFilename relativeToVRT="1">{t["path"]}</SourceFilename>\n      <SourceBand>{i}</SourceBand>\n      <SrcRect xOff="0" yOff="0" xSize="50000" ySize="50000"/>\n      <DstRect xOff="{x_off}" yOff="{y_off}" xSize="50000" ySize="50000"/>\n    </SimpleSource>\n')
            f.write(f'  </VRTRasterBand>\n')
        f.write('</VRTDataset>\n')

def main():
    if not get_access_token(): return
    
    # Load existing state once at start
    vrt_state = load_vrt_state()
    vrt_path = os.path.join(BASE_DOWNLOAD_DIR, VRT_FILENAME)
    
    for lat in range(55, 70):
        current_workers, current_delay = get_runtime_config()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Konfig: {current_workers} trådar, {current_delay}s delay.")
        
        # Refresh token before search
        token = get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        headers.update(UA_HEADERS)

        bbox_slice = [10.0, lat, 24.5, lat + 1.0]
        features = []
        
        # Search loop
        for attempt in range(MAX_RETRIES):
            try:
                session = get_session()
                r = session.post(LM_STAC_HOJD_URL, headers=headers, json={"bbox": bbox_slice, "limit": 10000}, timeout=30)
                if r.status_code == 401:
                    if os.path.exists(TOKEN_CACHE_FILE): os.remove(TOKEN_CACHE_FILE)
                    token = get_access_token()
                    headers["Authorization"] = f"Bearer {token}"
                    continue
                r.raise_for_status()
                features = r.json().get("features", [])
                print(f"Lat {lat}: {len(features)} filer hittade.")
                break 
            except Exception as e:
                print(f"Sökfel Lat {lat}: {e}")
                time.sleep(10)

        if not features: continue

        download_queue = []
        for f in features:
            href = f["assets"]["data"]["href"]
            
            # Pass bbox directly to worker so it can pass it back on success
            bbox = f["properties"]["proj:bbox"]
            
            # Pre-package the arguments
            # item = (url, headers, bbox)
            download_queue.append((href, UA_HEADERS.copy(), bbox))

        with ProcessPoolExecutor(max_workers=current_workers) as executor:
            # Submit all
            futures = {executor.submit(process_file, item, current_delay): item for item in download_queue}
            
            pbar = tqdm(as_completed(futures), total=len(download_queue))
            
            files_since_save = 0
            
            for future in pbar:
                res = future.result()
                
                # --- LIVE UPDATE LOGIC ---
                if res["status"] == "downloaded":
                    # 1. Update In-Memory State
                    vrt_state[res["path"]] = {"path": res["path"], "bbox": res["bbox"]}
                    
                    # 2. Update Disk State Immediately (Data Safety)
                    save_vrt_state_safe(vrt_state)
                    
                    # 3. Regenerate VRT (Operational View)
                    # We do this every 5 files to avoid excessive IO, or immediately if you prefer
                    files_since_save += 1
                    if files_since_save >= 5:
                        create_vrt_from_state(vrt_state, vrt_path)
                        files_since_save = 0
                        
                elif res["status"] == "skipped":
                    # Ensure skipped files are also in state (e.g. restart)
                    if res["path"] not in vrt_state:
                         vrt_state[res["path"]] = {"path": res["path"], "bbox": res["bbox"]}
                         # No need to save immediately for skipped, wait for next batch
                
                elif res["status"] == "error":
                    tqdm.write(f"Fel: {res['msg']}")

            # Final save/VRT update at end of latitude block
            save_vrt_state_safe(vrt_state)
            create_vrt_from_state(vrt_state, vrt_path)

    print("Nedladdning klar.")

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()