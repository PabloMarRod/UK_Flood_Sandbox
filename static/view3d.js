/* UK Flood Sandbox: 3D isometric view of the selected area (three.js r128).
   Terrain = the model's own corner heights; ground = land-cover texture; buildings = footprints with measured
   heights; water = a lit surface at bed + depth for every wet model cell, updated per frame. */
window.V3 = (() => {
  let renderer, scene, camera, root, terrain, water, waterGeo, markers, S3 = null, R3 = null;
  let mGroup = null, picker = null, raycaster = null, diffOn = null;
  let az = Math.PI / 4, el = 0.6155, zoom = 1, target = new THREE.Vector3(), ex = 1, bGroup = null, tGroup = null, showTrees = true, zmid0 = 0, layer = 'time', frame = 0;
  let heights, inside, NX, NY, H, W, D, cellBed, active = false, el3d;

  const dec = (b64, T) => { const s = atob(b64), u = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i); return new T(u.buffer); };
  const DEPTH = [[0.02, [198, 232, 240]], [0.1, [140, 205, 228]], [0.3, [66, 160, 206]], [0.6, [24, 108, 170]], [1.0, [12, 68, 128]], [1.8, [8, 36, 78]]];
  const SPEED = [[0.05, [250, 232, 200]], [0.5, [240, 190, 110]], [1, [214, 130, 50]], [2, [160, 70, 30]], [3, [90, 30, 20]]];
  function ramp(stops, v, out, o) {
    let c = stops[stops.length - 1][1];
    for (let i = 0; i < stops.length - 1; i++) {
      const [a, ca] = stops[i], [b, cb] = stops[i + 1];
      if (v <= a) { c = ca; break; }
      if (v <= b) { const t = (v - a) / (b - a); c = [0, 1, 2].map(k => ca[k] + t * (cb[k] - ca[k])); break; }
    }
    out[o] = c[0] / 255; out[o + 1] = c[1] / 255; out[o + 2] = c[2] / 255;
  }
  const hAt = (x, z) => {            // terrain height (bilinear) at local metres
    const fi = Math.max(0, Math.min(NX - 1e-6, x / H)), fj = Math.max(0, Math.min(NY - 1e-6, z / H));
    const i = Math.floor(fi), j = Math.floor(fj), a = fi - i, b = fj - j, w = NX + 1;
    return (1 - a) * (1 - b) * heights[j * w + i] + a * (1 - b) * heights[j * w + i + 1] + (1 - a) * b * heights[(j + 1) * w + i] + a * b * heights[(j + 1) * w + i + 1];
  };

  function init(container) {
    el3d = container;
    renderer = new THREE.WebGLRenderer({antialias: true, alpha: true, preserveDrawingBuffer: true});
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    container.appendChild(renderer.domElement);
    scene = new THREE.Scene();
    scene.add(new THREE.HemisphereLight(0xf2f6ff, 0x5a6a52, 0.75));
    const sun = new THREE.DirectionalLight(0xfff4e0, 0.75); sun.position.set(-0.6, 1, -0.4); scene.add(sun);
    camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 1, 20000);   // depth range fitted to the scene in placeCamera()
    controls(renderer.domElement);
    addEventListener('resize', resize); resize();
    (function loop() { requestAnimationFrame(loop); if (active) { renderer.render(scene, camera); placeLabels(); } })();
  }
  function resize() {
    if (!renderer || !el3d) return;
    const w = el3d.clientWidth || innerWidth, h = el3d.clientHeight || innerHeight;
    renderer.setSize(w, h, false); renderer.domElement.style.width = w + 'px'; renderer.domElement.style.height = h + 'px';
    placeCamera();
  }
  function placeCamera() {
    if (!camera) return;
    const w = el3d.clientWidth || innerWidth, h = el3d.clientHeight || innerHeight, span = Math.max(W || 1000, D || 1000) * 0.62 / zoom;
    camera.left = -span * w / h; camera.right = span * w / h; camera.top = span; camera.bottom = -span;
    // a tight depth range keeps thin water distinguishable from the ground at every angle
    const diag = Math.hypot(W || 1000, D || 1000) + 400 * ex, r = diag + 500;
    camera.near = Math.max(1, r - diag); camera.far = r + diag; camera.updateProjectionMatrix();
    const off = new THREE.Vector3(Math.sin(az) * Math.cos(el), Math.sin(el), Math.cos(az) * Math.cos(el)).multiplyScalar(r);
    camera.position.copy(target).add(off); camera.lookAt(target);
  }
  function panBy(dxw, dyw) {                                          // move the view across the ground (world metres on screen)
    const right = new THREE.Vector3(Math.cos(az), 0, -Math.sin(az)), fwd = new THREE.Vector3(-Math.sin(az), 0, -Math.cos(az));
    target.addScaledVector(right, dxw).addScaledVector(fwd, dyw / Math.max(Math.sin(el), 0.3)); placeCamera();
  }
  function controls(dom) {
    let drag = null;
    dom.addEventListener('pointerdown', e => { drag = {x: e.clientX, y: e.clientY, pan: e.button === 2 || e.shiftKey}; dom.setPointerCapture(e.pointerId); });
    dom.addEventListener('pointermove', e => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y; drag.x = e.clientX; drag.y = e.clientY;
      if (drag.pan) {
        const s = (camera.top - camera.bottom) / (el3d.clientHeight || innerHeight);
        panBy(-dx * s, dy * s);
        return;
      } else { az -= dx * 0.006; el = Math.max(0.2, Math.min(1.35, el + dy * 0.004)); }
      placeCamera();
    });
    let downAt = null;
    dom.addEventListener('pointerdown', e => { downAt = {x: e.clientX, y: e.clientY}; });
    dom.addEventListener('pointerup', e => {
      const moved = downAt ? Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y) : 99; drag = null;
      if (scanPick && moved < 5 && e.button === 0) { const p = pickScanAt(e.clientX, e.clientY); scanPick(p); return; }
      if (picker && moved < 5 && e.button === 0) { const p = groundAt(e.clientX, e.clientY); if (p) picker(p, e); }
    });
    dom.addEventListener('dblclick', e => { if (picker) picker(null, e); });
    dom.addEventListener('contextmenu', e => e.preventDefault());
    dom.addEventListener('wheel', e => {                              // zoom towards the point under the cursor
      e.preventDefault();
      const z1 = Math.max(0.4, Math.min(30, zoom * Math.exp(-e.deltaY * 0.0012))), r = dom.getBoundingClientRect();
      const nx = (e.clientX - r.left) / r.width * 2 - 1, ny = -((e.clientY - r.top) / r.height * 2 - 1);
      camera.updateMatrixWorld();
      const right = new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 0), up = new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 1);
      const p = target.clone().addScaledVector(right, nx * camera.right).addScaledVector(up, ny * camera.top);
      target.copy(p.clone().add(target.clone().sub(p).multiplyScalar(zoom / z1)));
      zoom = z1; placeCamera();
    }, {passive: false});
    addEventListener('keydown', e => {                                // arrow keys move the view
      if (!active || /INPUT|SELECT|TEXTAREA/.test((document.activeElement || {}).tagName || '') || document.querySelector('dialog[open]')) return;
      const k = {ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1]}[e.key];
      if (!k) return;
      e.preventDefault(); panBy(k[0] * camera.right * 0.15, k[1] * camera.top * 0.15);
    });
  }

  function build(sc) {
    S3 = sc; if (root) scene.remove(root);
    root = new THREE.Group(); scene.add(root);
    ({NX, NY, H} = sc); W = NX * H; D = NY * H;
    heights = dec(sc.heights, Float32Array); inside = dec(sc.inside, Uint8Array);
    const zmid = (sc.zmin + sc.zmax) / 2; root.position.set(-W / 2, -zmid, -D / 2); zmid0 = zmid;
    target.set(0, 0, 0); zoom = 1; root.scale.y = ex;
    cellBed = new Float32Array(NX * NY);
    for (let j = 0; j < NY; j++) for (let i = 0; i < NX; i++) {
      const w = NX + 1; cellBed[j * NX + i] = (heights[j * w + i] + heights[j * w + i + 1] + heights[(j + 1) * w + i] + heights[(j + 1) * w + i + 1]) / 4;
    }
    // terrain surface + earth skirt round the outline (a diorama base)
    const nv = (NX + 1) * (NY + 1), pos = new Float32Array(nv * 3), uv = new Float32Array(nv * 2);
    const gw = sc.ground_size[0] * sc.ground_res, gh = sc.ground_size[1] * sc.ground_res;
    for (let j = 0; j <= NY; j++) for (let i = 0; i <= NX; i++) {
      const k = j * (NX + 1) + i; pos[k * 3] = i * H; pos[k * 3 + 1] = heights[k]; pos[k * 3 + 2] = j * H;
      uv[k * 2] = (i * H) / gw; uv[k * 2 + 1] = 1 - (j * H) / gh;
    }
    const idx = [];
    for (let j = 0; j < NY; j++) for (let i = 0; i < NX; i++) if (inside[j * NX + i]) {
      const a = j * (NX + 1) + i, b = a + 1, c = a + NX + 1, d = c + 1; idx.push(a, c, b, b, c, d);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3)); g.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
    g.setIndex(idx); g.computeVertexNormals();
    const tex = new THREE.TextureLoader().load(sc.ground); tex.anisotropy = 4;
    terrain = new THREE.Mesh(g, new THREE.MeshLambertMaterial({map: tex, polygonOffset: true, polygonOffsetFactor: 2, polygonOffsetUnits: 4})); root.add(terrain);
    const base = sc.zmin - Math.max(8, (sc.zmax - sc.zmin) * 0.15), sk = [];
    const sideQuad = (x1, z1, h1, x2, z2, h2) => sk.push(x1, h1, z1, x2, h2, z2, x2, base, z2, x1, h1, z1, x2, base, z2, x1, base, z1);
    for (let j = 0; j < NY; j++) for (let i = 0; i < NX; i++) if (inside[j * NX + i]) {
      const w = NX + 1, hh = (jj, ii) => heights[jj * w + ii], out = (jj, ii) => jj < 0 || ii < 0 || jj >= NY || ii >= NX || !inside[jj * NX + ii];
      if (out(j - 1, i)) sideQuad((i + 1) * H, j * H, hh(j, i + 1), i * H, j * H, hh(j, i));
      if (out(j + 1, i)) sideQuad(i * H, (j + 1) * H, hh(j + 1, i), (i + 1) * H, (j + 1) * H, hh(j + 1, i + 1));
      if (out(j, i - 1)) sideQuad(i * H, j * H, hh(j, i), i * H, (j + 1) * H, hh(j + 1, i));
      if (out(j, i + 1)) sideQuad((i + 1) * H, (j + 1) * H, hh(j + 1, i + 1), (i + 1) * H, j * H, hh(j, i + 1));
    }
    const sg = new THREE.BufferGeometry(); sg.setAttribute('position', new THREE.BufferAttribute(new Float32Array(sk), 3)); sg.computeVertexNormals();
    root.add(new THREE.Mesh(sg, new THREE.MeshLambertMaterial({color: 0x7a6650, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 2, polygonOffsetUnits: 4})));
    zmid0 = zmid; buildBuildings(); buildIfc(); buildTrees();
    // defences as walls up to their crest
    const dp = [];
    for (const d of sc.defences) for (let k = 0; k < d.l.length - 1; k++) {
      const [x1, z1] = d.l[k], [x2, z2] = d.l[k + 1], g1 = hAt(x1, z1), g2 = hAt(x2, z2), c1 = Math.max(d.z, g1 + 0.3), c2 = Math.max(d.z, g2 + 0.3);
      dp.push(x1, g1, z1, x2, g2, z2, x2, c2, z2, x1, g1, z1, x2, c2, z2, x1, c1, z1);
    }
    if (dp.length) {
      const dg = new THREE.BufferGeometry(); dg.setAttribute('position', new THREE.BufferAttribute(new Float32Array(dp), 3)); dg.computeVertexNormals();
      root.add(new THREE.Mesh(dg, new THREE.MeshLambertMaterial({color: 0x6b3fa0, side: THREE.DoubleSide, transparent: true, opacity: 0.85})));
    }
    markers = new THREE.Group(); root.add(markers);
    const pin = (x, z, color, size) => {
      const m = new THREE.Mesh(new THREE.ConeGeometry(size * 0.45, size, 12), new THREE.MeshLambertMaterial({color}));
      m.rotation.x = Math.PI; m.position.set(x, hAt(x, z) + size * 0.5 + 1, z); markers.add(m);
    };
    const ps = Math.max(W, D) / 60;
    sc.inflows.forEach(f => pin(f.at[0], f.at[1], 0x185f9e, ps)); sc.exits.forEach(f => pin(f.at[0], f.at[1], 0x16303a, ps));
    // water surface (geometry reused per frame)
    waterGeo = new THREE.BufferGeometry();
    waterGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(nv * 3), 3));
    waterGeo.setAttribute('color', new THREE.BufferAttribute(new Float32Array(nv * 3), 3));
    // matte water: a shiny surface reflected the sun towards some view angles and washed thin water out
    water = new THREE.Mesh(waterGeo, new THREE.MeshLambertMaterial({vertexColors: true, transparent: true, opacity: 0.9,
                                                                    emissive: 0x0b2236, side: THREE.DoubleSide, depthWrite: false}));
    water.renderOrder = 2; root.add(water);
    placeCamera(); if (R3) setResult(R3);
  }

  // buildings at their real heights: only the ground is exaggerated, so they are rebuilt when that changes
  function buildBuildings() {
    if (!S3) return;
    if (bGroup) scene.remove(bGroup);
    bGroup = new THREE.Group(); bGroup.position.set(-W / 2, 0, -D / 2); scene.add(bGroup);
    const bp = [], bc = [];
    let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    for (const b of S3.buildings) {
      const ring = b.p, base = ex * b.b - zmid0, top = base + b.h, n = ring.length;
      const t = 0.92 + 0.16 * rnd(), warm = rnd() < 0.45;            // gentle variety: stone, brick-ish, grey
      const wall = warm ? [0.80 * t, 0.70 * t, 0.62 * t] : [0.84 * t, 0.83 * t, 0.80 * t];
      const roof = rnd() < 0.5 ? [0.46 * t, 0.47 * t, 0.50 * t] : [0.58 * t, 0.49 * t, 0.44 * t];
      for (let k = 0; k < n; k++) {
        const [x1, z1] = ring[k], [x2, z2] = ring[(k + 1) % n];
        bp.push(x1, base, z1, x2, base, z2, x2, top, z2, x1, base, z1, x2, top, z2, x1, top, z1);
        for (let q = 0; q < 6; q++) bc.push(...wall);
      }
      const tri = THREE.ShapeUtils.triangulateShape(ring.map(([x, z]) => new THREE.Vector2(x, z)), []);
      for (const [a, b2, c] of tri) for (const q of [a, c, b2]) { bp.push(ring[q][0], top, ring[q][1]); bc.push(...roof); }
    }
    if (!bp.length) return;
    const bg = new THREE.BufferGeometry(); bg.setAttribute('position', new THREE.BufferAttribute(new Float32Array(bp), 3));
    bg.setAttribute('color', new THREE.BufferAttribute(new Float32Array(bc), 3)); bg.computeVertexNormals();
    bGroup.add(new THREE.Mesh(bg, new THREE.MeshLambertMaterial({vertexColors: true, side: THREE.DoubleSide})));
  }

  // imported IFC buildings: their own triangles, at true height above their base (only the ground is exaggerated)
  let ifcGroup = null;
  const IFC_COL = {IfcWall: 0xd9d2c5, IfcWallStandardCase: 0xd9d2c5, IfcCurtainWall: 0x9fb8c8, IfcSlab: 0xa9a49b,
                   IfcRoof: 0x7a6f66, IfcDoor: 0x8a5a2b, IfcColumn: 0xbdb6aa};
  let labels = [], labelBox = null, pins = [];
  const pinScale = () => 1 / Math.max(1, zoom / 1.2);                 // pins keep about the same size on screen
  function placeLabels() {                                            // HTML name labels follow their pins
    if (!labels.length || !camera) return;
    const k = pinScale();
    for (const g of pins) g.scale.setScalar(k);
    for (const L_ of labels) L_.at.copy(L_.base).setY(L_.base.y + L_.rise * k);
    const r = el3d.getBoundingClientRect();
    for (const L_ of labels) {
      const v = L_.at.clone().project(camera);
      const off = v.z > 1 || Math.abs(v.x) > 1.05 || Math.abs(v.y) > 1.05;
      L_.el.style.display = off ? 'none' : '';
      if (!off) L_.el.style.transform = `translate(${(v.x + 1) / 2 * r.width}px, ${(1 - v.y) / 2 * r.height}px) translate(-50%, -100%)`;
    }
  }
  let scanPick = null, pickMarks = null;
  function pickScanAt(cx, cy) {                                       // the scanned point under the mouse, in BNG and m AOD
    if (!ifcGroup) return null;
    raycaster = raycaster || new THREE.Raycaster();
    const r = renderer.domElement.getBoundingClientRect();
    raycaster.params.Points = {threshold: Math.max(0.15, (camera.top - camera.bottom) / r.height * 4)};
    raycaster.setFromCamera(new THREE.Vector2((cx - r.left) / r.width * 2 - 1, -(cy - r.top) / r.height * 2 + 1), camera);
    const hits = raycaster.intersectObjects(ifcGroup.children.filter(o => o.userData.scan));
    if (!hits.length) return null;
    const h = hits[0], pa = h.object.geometry.attributes.position, i = h.index;
    return {id: h.object.userData.ifc, E: S3.X0 + pa.getX(i), N: S3.Y1 - pa.getZ(i), z: pa.getY(i) - h.object.userData.dy};
  }
  function showMarks(list) {                                          // marks being placed, before they are saved
    if (pickMarks) { scene.remove(pickMarks); pickMarks = null; }
    if (!list || !list.length || !S3) return;
    pickMarks = new THREE.Group(); pickMarks.position.set(-W / 2, 0, -D / 2); scene.add(pickMarks);
    const b = (S3.ifc || [])[0];
    for (const o of list) {
      const bb = (S3.ifc || []).find(x => x.id === o.id) || b, dy = ex * bb.base - zmid0 - bb.base;
      const m = new THREE.Mesh(new THREE.BoxGeometry(Math.max(0.3, o.width || 0.9), o.kind === 'door' ? 2.0 : 1.1, 0.3),
                               new THREE.MeshLambertMaterial({color: o.kind === 'door' ? 0xd07b2a : 0x2f7fd1, transparent: true, opacity: 0.85}));
      m.position.set(o.E - S3.X0, o.z + dy + (o.kind === 'door' ? 1.0 : 0.55), S3.Y1 - o.N); pickMarks.add(m);
    }
  }
  function focusIfc(id, z) {
    const b = (S3 && S3.ifc || []).find(x => x.id === id); if (!b) return false;
    let sx = 0, sz = 0, n = 0;
    for (const p of b.parts) { const a = p.v || p.p; for (let k = 0; k < a.length; k += 3) { sx += a[k]; sz += a[k + 2]; n++; } }
    if (!n) return false;
    target.set(sx / n - W / 2, ex * b.base - zmid0, sz / n - D / 2); zoom = z || 7; placeCamera(); return true;
  }
  function buildIfc() {
    if (!S3) return;
    for (const L_ of labels) L_.el.remove(); labels = []; pins = [];
    if (!labelBox) { labelBox = document.createElement('div'); labelBox.style.cssText = 'position:absolute;inset:0;pointer-events:none;overflow:hidden'; el3d.appendChild(labelBox); }
    if (ifcGroup) scene.remove(ifcGroup);
    ifcGroup = new THREE.Group(); ifcGroup.position.set(-W / 2, 0, -D / 2); scene.add(ifcGroup);
    for (const b of S3.ifc || []) {
      const dy = ex * b.base - zmid0 - b.base;                       // y = ex*base - zmid + (z - base)
      for (const p of b.parts) {
        if (p.p) {                                                    // a scan: its points, in its own colours
          const v = new Float32Array(p.p.length);
          for (let k = 0; k < p.p.length; k += 3) { v[k] = p.p[k]; v[k + 1] = p.p[k + 1] + dy; v[k + 2] = p.p[k + 2]; }
          const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.BufferAttribute(v, 3));
          if (p.rgb && p.rgb.length === p.p.length) g.setAttribute('color', new THREE.BufferAttribute(new Float32Array(p.rgb), 3));
          const pts = new THREE.Points(g, new THREE.PointsMaterial({size: 2.2, sizeAttenuation: false, vertexColors: !!(p.rgb && p.rgb.length), color: p.rgb && p.rgb.length ? 0xffffff : 0xd2c8b8}));
          pts.userData = {ifc: b.id, scan: true, dy}; ifcGroup.add(pts); continue;
        }
        const v = new Float32Array(p.v.length);
        for (let k = 0; k < p.v.length; k += 3) { v[k] = p.v[k]; v[k + 1] = p.v[k + 1] + dy; v[k + 2] = p.v[k + 2]; }
        const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.BufferAttribute(v, 3));
        g.setIndex(p.f); g.computeVertexNormals();
        const mesh = new THREE.Mesh(g, new THREE.MeshLambertMaterial({color: IFC_COL[p.c] || 0xc8c2b8, side: THREE.DoubleSide}));
        mesh.userData.ifc = b.id; ifcGroup.add(mesh);
      }
      // a tall pin above the building and its name, so it can be found at any zoom
      let sx = 0, sz = 0, top = -1e9, n = 0;
      for (const p of b.parts) { const a = p.v || p.p; for (let k = 0; k < a.length; k += 3) { sx += a[k]; sz += a[k + 2]; top = Math.max(top, a[k + 1]); n++; } }
      if (!n) continue;
      for (const o of b.openings || []) {                             // doors and windows marked on a scan
        const m = new THREE.Mesh(new THREE.BoxGeometry(0.9, o.kind === 'door' ? 2.0 : 1.1, 0.25),
                                 new THREE.MeshLambertMaterial({color: o.kind === 'door' ? 0x8a5a2b : 0x4f86b5}));
        m.position.set(o.x, o.y + dy + (o.kind === 'door' ? 1.0 : 0.55), o.z); ifcGroup.add(m);
      }
      const cx = sx / n, cz = sz / n, ytop = top + dy, ps = Math.max(W, D) / 45;
      const pinG = new THREE.Group(); pinG.position.set(cx, ytop, cz); ifcGroup.add(pinG); pins.push(pinG);
      const stem = new THREE.Mesh(new THREE.CylinderGeometry(ps * 0.04, ps * 0.04, ps * 1.6, 8), new THREE.MeshLambertMaterial({color: 0x5b3fa0}));
      stem.position.set(0, ps * 0.8, 0); pinG.add(stem);
      const head = new THREE.Mesh(new THREE.ConeGeometry(ps * 0.35, ps * 0.7, 16), new THREE.MeshLambertMaterial({color: 0x7d5fd0}));
      head.rotation.x = Math.PI; head.position.set(0, ps * 1.6 + ps * 0.35, 0); pinG.add(head);
      const el = document.createElement('button'); el.type = 'button'; el.textContent = b.name || 'IFC building';
      el.title = 'Zoom to this building';
      el.style.cssText = 'position:absolute;left:0;top:0;pointer-events:auto;background:#5b3fa0;color:#fff;border:0;border-radius:6px;padding:3px 8px;font:600 12px/1.3 system-ui,sans-serif;white-space:nowrap;cursor:pointer;box-shadow:0 2px 6px rgba(0,0,0,.25)';
      el.onclick = () => focusIfc(b.id);
      labelBox.appendChild(el);
      labels.push({el, base: new THREE.Vector3(cx - W / 2, ytop, cz - D / 2), rise: ps * 2.4, at: new THREE.Vector3()});
    }
  }

  // ground point under the mouse, in local metres [x east, z south] (null if off the model)
  function groundAt(cx, cy) {
    if (!terrain) return null;
    raycaster = raycaster || new THREE.Raycaster();
    const r = renderer.domElement.getBoundingClientRect();
    raycaster.setFromCamera(new THREE.Vector2((cx - r.left) / r.width * 2 - 1, -(cy - r.top) / r.height * 2 + 1), camera);
    const hit = raycaster.intersectObject(terrain)[0];
    if (!hit) return null;
    const p = root.worldToLocal(hit.point.clone());
    return [Math.round(p.x * 10) / 10, Math.round(p.z * 10) / 10];
  }

  // user-placed defences (local coordinates), plus the one being drawn
  const MCOL = {wall: 0xc8742a, barrier: 0xe0b040, embankment: 0x8a7a4a, basin: 0x41606e, kerb: 0x8d8d8d, deepen: 0x2f6f9a,
                permeable: 0xd4a83a, raingarden: 0x3f9a4a, swale: 0x2f9f9a, trees: 0x2f6f2f, greenroof: 0x7fc04a,
                tank: 0x34495e, pump: 0x7d3c98, gate: 0xb0413e};
  const AREAS = new Set(['basin', 'permeable', 'raingarden', 'trees', 'deepen', 'greenroof']);
  function drawMeasures(list, draft) {
    if (!root) return;
    if (mGroup) root.remove(mGroup);
    mGroup = new THREE.Group(); root.add(mGroup);
    const strip = (pts, hgt, colour, opacity, width) => {
      const p = [];
      for (let k = 0; k < pts.length - 1; k++) {
        const [x1, z1] = pts[k], [x2, z2] = pts[k + 1], g1 = hAt(x1, z1), g2 = hAt(x2, z2);
        if (width) {                                          // embankment: a raised band
          const dx = x2 - x1, dz = z2 - z1, L = Math.hypot(dx, dz) || 1, nx = -dz / L * width / 2, nz = dx / L * width / 2;
          p.push(x1 + nx, g1 + hgt, z1 + nz, x2 + nx, g2 + hgt, z2 + nz, x2 - nx, g2 + hgt, z2 - nz,
                 x1 + nx, g1 + hgt, z1 + nz, x2 - nx, g2 + hgt, z2 - nz, x1 - nx, g1 + hgt, z1 - nz);
        } else {
          p.push(x1, g1, z1, x2, g2, z2, x2, g2 + hgt, z2, x1, g1, z1, x2, g2 + hgt, z2, x1, g1 + hgt, z1);
        }
      }
      const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(p), 3)); g.computeVertexNormals();
      mGroup.add(new THREE.Mesh(g, new THREE.MeshLambertMaterial({color: colour, side: THREE.DoubleSide, transparent: opacity < 1, opacity})));
    };
    const outlineFor = (pts, colour, closed) => {
      const arr = (closed ? pts.concat([pts[0]]) : pts).map(([x, z]) => new THREE.Vector3(x, hAt(x, z) + 0.4, z));
      mGroup.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(arr), new THREE.LineBasicMaterial({color: colour})));
    };
    const drape = (pts, colour, opacity, lift) => {        // a translucent area following the ground (subdivided)
      const tri = THREE.ShapeUtils.triangulateShape(pts.map(([x, z]) => new THREE.Vector2(x, z)), []), p = [];
      const push = (x, z) => p.push(x, hAt(x, z) + lift, z), step = H * 1.5;
      for (const [a, b, c] of tri) {
        const A_ = pts[a], B = pts[c], C = pts[b], n = Math.max(1, Math.ceil(Math.max(Math.hypot(B[0] - A_[0], B[1] - A_[1]), Math.hypot(C[0] - A_[0], C[1] - A_[1])) / step));
        for (let u = 0; u < n; u++) for (let w = 0; w < n - u; w++) {
          const P = (uu, ww) => [A_[0] + (B[0] - A_[0]) * uu / n + (C[0] - A_[0]) * ww / n, A_[1] + (B[1] - A_[1]) * uu / n + (C[1] - A_[1]) * ww / n];
          for (const q of [[u, w], [u + 1, w], [u, w + 1]]) push(...P(...q));
          if (w < n - u - 1) for (const q of [[u + 1, w], [u + 1, w + 1], [u, w + 1]]) push(...P(...q));
        }
      }
      const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(p), 3)); g.computeVertexNormals();
      mGroup.add(new THREE.Mesh(g, new THREE.MeshLambertMaterial({color: colour, transparent: true, opacity, side: THREE.DoubleSide, depthWrite: false})));
    };
    for (const m of list) {
      if (AREAS.has(m.type)) {
        drape(m.pts, MCOL[m.type], m.type === 'basin' ? 0.75 : 0.62, m.type === 'basin' ? 0.2 : 0.5);
        strip(m.pts.concat([m.pts[0]]), Math.max(2, Math.max(W, D) / 400), MCOL[m.type], 0.55, 0);   // coloured edge ribbon
        outlineFor(m.pts, 0x16303a, true);
      } else if (m.type === 'tank' || m.type === 'pump') {
        const [x, z] = m.pts[0], r = Math.max(4, Math.max(W, D) / 160), hgt = Math.max(3, Math.max(W, D) / 200);
        const g = m.type === 'tank' ? new THREE.CylinderGeometry(r, r, hgt, 20) : new THREE.BoxGeometry(r * 1.4, hgt, r * 1.4);
        const mesh = new THREE.Mesh(g, new THREE.MeshLambertMaterial({color: MCOL[m.type]})); mesh.position.set(x, hAt(x, z) + hgt / 2, z); mGroup.add(mesh);
        if (m.type === 'pump' && m.pts.length > 1) {
          const [x2, z2] = m.pts[1], arr = [new THREE.Vector3(x, hAt(x, z) + hgt, z), new THREE.Vector3((x + x2) / 2, Math.max(hAt(x, z), hAt(x2, z2)) + hgt * 3, (z + z2) / 2), new THREE.Vector3(x2, hAt(x2, z2) + 1, z2)];
          const curve = new THREE.QuadraticBezierCurve3(...arr);
          mGroup.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(curve.getPoints(24)), new THREE.LineDashedMaterial({color: MCOL.pump, dashSize: 6, gapSize: 4})));
        }
      } else if (m.type === 'swale') { strip(m.pts, 0.5, MCOL.swale, 0.85, m.width); strip(m.pts, Math.max(2, Math.max(W, D) / 400), MCOL.swale, 0.55, 0); }
      else strip(m.pts, m.height, MCOL[m.type], m.type === 'barrier' ? 0.85 : 1, m.type === 'embankment' ? m.width : 0);
    }
    if (draft && draft.pts.length) {
      outlineFor(draft.pts, 0xb03a2e, AREAS.has(draft.type) && draft.pts.length > 2);
      for (const [x, z] of draft.pts) {
        const s = new THREE.Mesh(new THREE.SphereGeometry(Math.max(W, D) / 260, 10, 8), new THREE.MeshBasicMaterial({color: 0xb03a2e}));
        s.position.set(x, hAt(x, z) + 0.6, z); mGroup.add(s);
      }
    }
  }

  // difference in maximum depth against another run: green shallower, red deeper
  function showDiff(cur, ref) {
    if (!S3 || !waterGeo) return;
    const w = NX + 1, nv = w * (NY + 1), pos = waterGeo.attributes.position.array, col = waterGeo.attributes.color.array;
    const dsum = new Float32Array(nv), cnt = new Uint8Array(nv), idx = [];
    for (let j = 0; j < NY; j++) for (let i = 0; i < NX; i++) {
      const c = j * NX + i, d = (cur[c] - ref[c]) * 0.02;
      if (Math.abs(d) < 0.03) continue;
      for (const k of [j * w + i, j * w + i + 1, (j + 1) * w + i, (j + 1) * w + i + 1]) { dsum[k] += d; cnt[k]++; }
      const a = j * w + i; idx.push(a, a + w, a + 1, a + 1, a + w, a + w + 1);
    }
    for (let k = 0; k < nv; k++) {
      const i = k % w, j = (k / w) | 0, d = cnt[k] ? dsum[k] / cnt[k] : 0, t = Math.min(1, Math.abs(d) / 0.5);
      pos[k * 3] = i * H; pos[k * 3 + 2] = j * H; pos[k * 3 + 1] = heights[k] + (cnt[k] ? 0.15 : -1);
      const c = d < 0 ? [0.85 - 0.6 * t, 0.92 - 0.25 * t, 0.85 - 0.55 * t] : [0.95 - 0.1 * t, 0.85 - 0.6 * t, 0.8 - 0.6 * t];
      col[k * 3] = c[0]; col[k * 3 + 1] = c[1]; col[k * 3 + 2] = c[2];
    }
    waterGeo.setIndex(idx); waterGeo.attributes.position.needsUpdate = true; waterGeo.attributes.color.needsUpdate = true; waterGeo.computeVertexNormals();
  }

  // trees: instanced crowns and trunks at measured heights (rebuilt when the ground exaggeration changes)
  function buildTrees() {
    if (tGroup) { scene.remove(tGroup); tGroup = null; }
    if (!S3 || !S3.trees || !S3.trees.length || !showTrees) return;
    tGroup = new THREE.Group(); tGroup.position.set(-W / 2, 0, -D / 2); scene.add(tGroup);
    const n = S3.trees.length, crownG = new THREE.IcosahedronGeometry(1, 1), trunkG = new THREE.CylinderGeometry(0.12, 0.16, 1, 6);
    const crowns = new THREE.InstancedMesh(crownG, new THREE.MeshLambertMaterial({color: 0xffffff}), n);
    const trunks = new THREE.InstancedMesh(trunkG, new THREE.MeshLambertMaterial({color: 0x6b5340}), n);
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), sc = new THREE.Vector3(), p = new THREE.Vector3(), c = new THREE.Color();
    let seed = 11; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    S3.trees.forEach(([x, z, g, h], k) => {
      const base = ex * g - zmid0, r = Math.max(1.4, Math.min(6, h * 0.32)), crownH = Math.max(2, h * 0.62);
      p.set(x, base + h - crownH / 2, z); sc.set(r, crownH / 2, r); m.compose(p, q, sc); crowns.setMatrixAt(k, m);
      const tl = Math.max(1, h - crownH * 0.8); p.set(x, base + tl / 2, z); sc.set(1, tl, 1); m.compose(p, q, sc); trunks.setMatrixAt(k, m);
      const t = 0.8 + 0.35 * rnd(); c.setRGB(0.30 * t, (0.46 + 0.08 * rnd()) * t, 0.22 * t); crowns.setColorAt(k, c);
    });
    crowns.instanceMatrix.needsUpdate = true; trunks.instanceMatrix.needsUpdate = true; if (crowns.instanceColor) crowns.instanceColor.needsUpdate = true;
    tGroup.add(trunks); tGroup.add(crowns);
  }

  function showCells(depth, colourBy) {
    if (!S3 || !waterGeo) return;
    const w = NX + 1, nv = w * (NY + 1), pos = waterGeo.attributes.position.array, col = waterGeo.attributes.color.array;
    const sum = new Float32Array(nv), cnt = new Uint8Array(nv), dsum = new Float32Array(nv), csum = new Float32Array(nv), idx = [];
    for (let j = 0; j < NY; j++) for (let i = 0; i < NX; i++) {
      const c = j * NX + i, d = depth[c] * 0.02;
      if (d < 0.02) continue;
      const s = cellBed[c] + d, cv = colourBy ? colourBy[c] : d;
      for (const k of [j * w + i, j * w + i + 1, (j + 1) * w + i, (j + 1) * w + i + 1]) { sum[k] += s; dsum[k] += d; csum[k] += cv; cnt[k]++; }
      const a = j * w + i; idx.push(a, a + w, a + 1, a + 1, a + w, a + w + 1);
    }
    for (let k = 0; k < nv; k++) {
      const i = k % w, j = (k / w) | 0, n = cnt[k];
      pos[k * 3] = i * H; pos[k * 3 + 2] = j * H;
      pos[k * 3 + 1] = n ? Math.max(sum[k] / n, heights[k] + 0.05) : heights[k] - 1;   // never level with the ground
      if (colourBy) ramp(SPEED, n ? csum[k] / n : 0, col, k * 3); else ramp(DEPTH, n ? dsum[k] / n : 0, col, k * 3);
    }
    waterGeo.setIndex(idx); waterGeo.attributes.position.needsUpdate = true; waterGeo.attributes.color.needsUpdate = true;
    waterGeo.computeVertexNormals();
  }
  function setResult(res) {
    R3 = res; if (!S3 || !res || !res.cells) { if (waterGeo) waterGeo.setIndex([]); return; }
    if (res.cells.NX !== NX || res.cells.NY !== NY) return;
    R3._frames = res.cells.depth_u8.map(b => dec(b, Uint8Array));
    R3._maxd = dec(res.cells.maxd_u8, Uint8Array); R3._maxv = dec(res.cells.maxv_u8, Uint8Array);
    if (markers) {
      markers.children.filter(m => m.userData.burst).forEach(m => markers.remove(m));
      if (res.params && res.params.burst && res.params.burst_cell) {
        const [j, i] = res.params.burst_cell, x = (i + 0.5) * H, z = (j + 0.5) * H;
        const s = new THREE.Mesh(new THREE.SphereGeometry(Math.max(W, D) / 140, 16, 12), new THREE.MeshLambertMaterial({color: 0x8e3b46}));
        s.position.set(x, hAt(x, z) + Math.max(W, D) / 140, z); s.userData.burst = true; markers.add(s);
      }
    }
    redraw();
  }
  // categorical cells (hazard classes or EA map bands) as a coloured layer just above the ground
  function showClasses(u8, palette) {
    if (!S3 || !waterGeo || !u8) return;
    const w = NX + 1, nv = w * (NY + 1), pos = waterGeo.attributes.position.array, col = waterGeo.attributes.color.array;
    const best = new Uint8Array(nv), idx = [];
    for (let j = 0; j < NY; j++) for (let i = 0; i < NX; i++) {
      const c = u8[j * NX + i]; if (!c) continue;
      for (const k of [j * w + i, j * w + i + 1, (j + 1) * w + i, (j + 1) * w + i + 1]) best[k] = Math.max(best[k], c);
      const a = j * w + i; idx.push(a, a + w, a + 1, a + 1, a + w, a + w + 1);
    }
    for (let k = 0; k < nv; k++) {
      const i = k % w, j = (k / w) | 0, p = palette[best[k]] || [1, 1, 1];
      pos[k * 3] = i * H; pos[k * 3 + 2] = j * H; pos[k * 3 + 1] = heights[k] + (best[k] ? 0.15 : -1);
      col[k * 3] = p[0]; col[k * 3 + 1] = p[1]; col[k * 3 + 2] = p[2];
    }
    waterGeo.setIndex(idx); waterGeo.attributes.position.needsUpdate = true; waterGeo.attributes.color.needsUpdate = true; waterGeo.computeVertexNormals();
  }
  const HAZP = [null, [0.98, 0.86, 0.47], [0.94, 0.59, 0.24], [0.80, 0.27, 0.20], [0.47, 0.08, 0.24]];
  const EAP = [null, [0.76, 0.88, 1.0], [0.60, 0.62, 0.87], [0.33, 0.36, 0.62]];
  let eaCells = null;

  function redraw() {
    if (layer === 'diff' && diffOn) { showDiff(diffOn[0], diffOn[1]); return; }
    if (layer === 'hazard' && R3 && R3.cells && R3.cells.hazard_u8) { showClasses(dec(R3.cells.hazard_u8, Uint8Array), HAZP); return; }
    if (layer === 'ea') { if (eaCells) showClasses(eaCells, EAP); else if (waterGeo) waterGeo.setIndex([]); return; }
    if (!R3 || !R3._frames) return;
    if (layer === 'time') showCells(R3._frames[Math.min(frame, R3._frames.length - 1)]);
    else if (layer === 'maxd') showCells(R3._maxd);
    else { const v = new Float32Array(R3._maxv.length); R3._maxv.forEach((x, k) => v[k] = x * 0.05); showCells(R3._maxd, v); }
  }
  return {
    init, build, setResult,
    setFrame(k) { frame = k; if (layer === 'time') redraw(); },
    setLayer(l) { layer = l; redraw(); },
    focusIfc(id, z) { return focusIfc(id, z); },
    pickScan(cb) { scanPick = cb || null; },
    project(E, N, z, id) {                                            // screen position of a point (BNG, m AOD)
      const b = (S3.ifc || []).find(x => x.id === id) || {base: z}, dy = ex * b.base - zmid0 - b.base;
      const v = new THREE.Vector3(E - S3.X0 - W / 2, z + dy, S3.Y1 - N - D / 2).project(camera), r = renderer.domElement.getBoundingClientRect();
      return [r.left + (v.x + 1) / 2 * r.width, r.top + (1 - v.y) / 2 * r.height];
    },                           // click a scanned point: cb({id, E, N, z}) or cb(null)
    showMarks(list) { showMarks(list); },
    ifcList() { return (S3 && S3.ifc || []).map(b => ({id: b.id, name: b.name})); },
    focus(lx, lz, base, z) {                                          // centre on a point (local metres), e.g. a building
      if (!target) return; target.set(lx - W / 2, ex * base - zmid0, lz - D / 2); if (z) zoom = z; placeCamera();
      if (typeof requestRender === 'function') requestRender(); else if (renderer && scene && camera) renderer.render(scene, camera);
    },
    setExaggeration(v) { ex = v; if (root) { root.scale.y = v; buildBuildings(); buildIfc(); buildTrees(); } },
    setTrees(on) { showTrees = on; buildTrees(); },
    setEA(b64) { eaCells = b64 ? dec(b64, Uint8Array) : null; if (layer === 'ea') redraw(); },
    setPicker(fn) { picker = fn; if (renderer) renderer.domElement.style.cursor = fn ? 'crosshair' : 'grab'; },
    drawMeasures, groundAt,
    setDiff(cur_b64, ref_b64) { diffOn = cur_b64 && ref_b64 ? [dec(cur_b64, Uint8Array), dec(ref_b64, Uint8Array)] : null; if (layer === 'diff') redraw(); },
    get grid() { return S3 ? {X0: S3.X0, Y1: S3.Y1, NX, NY} : null; },
    setBuildings(on) { if (bGroup) bGroup.visible = on; },
    rotate(step) { az += step * Math.PI / 2; placeCamera(); },
    reset() { az = Math.PI / 4; el = 0.6155; zoom = 1; target.set(0, 0, 0); placeCamera(); },
    show(on) { active = on; if (on) resize(); },
    get active() { return active; }, get scene() { return S3; },
    snapshot() { renderer.render(scene, camera); return renderer.domElement.toDataURL('image/png'); },
  };
})();
