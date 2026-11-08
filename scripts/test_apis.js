async function testAll() {
  const tests = [
    { name: 'Summary', url: 'http://localhost:3000/api/hotspots/summary?start=2026-10-20T00:00&end=2026-10-22T23:59' },
    { name: 'Hotspots Grid', url: 'http://localhost:3000/api/hotspots?start=2026-10-20T00:00&end=2026-10-22T23:59&tier=grid&cell=0.05' },
    { name: 'Spread Origin Only', url: 'http://localhost:3000/api/hotspots/spread?start=2026-10-20T00:00&end=2026-10-22T23:59&layers=origin' },
    { name: 'Spread All Layers', url: 'http://localhost:3000/api/hotspots/spread?start=2026-10-20T00:00&end=2026-10-22T23:59&layers=origin,arrows,grid' },
    { name: 'Hotspots Detail', url: 'http://localhost:3000/api/hotspots/detail?lat=-2.15157&lon=112.16685' },
  ];

  for (const t of tests) {
    const t0 = performance.now();
    try {
      const res = await fetch(t.url);
      const isJson = res.headers.get('content-type')?.includes('application/json');
      const body = isJson ? await res.json() : await res.arrayBuffer();
      const dur = Math.round(performance.now() - t0);
      const size = isJson ? JSON.stringify(body).length : body.byteLength;
      console.log(`[PASS] ${t.name.padEnd(20)}: HTTP ${res.status} | ${dur}ms | ${size} bytes`);
    } catch (err) {
      console.error(`[FAIL] ${t.name}: ${err.message}`);
    }
  }
}
testAll();
