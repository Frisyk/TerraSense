import urllib.request
import json

for lid in [10, 2, 0, 3]:
    url = f"https://gis-gfw.wri.org/arcgis/rest/services/country_data/asia/MapServer/{lid}?f=json"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    print(f"=== Layer {lid}: {data.get('name')} ===")
    print("Fields:", [f['name'] for f in data.get('fields', [])])
    
    # Ambil 1 sample record
    q_url = f"https://gis-gfw.wri.org/arcgis/rest/services/country_data/asia/MapServer/{lid}/query?where=1%3D1&resultRecordCount=1&outFields=*&returnGeometry=false&f=json"
    q_req = urllib.request.Request(q_url, headers={'User-Agent': 'Mozilla/5.0'})
    q_data = json.loads(urllib.request.urlopen(q_req, timeout=15).read())
    if q_data.get('features'):
        print("Sample attrs:", q_data['features'][0]['attributes'])
    print()
