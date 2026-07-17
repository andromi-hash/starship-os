/* Memory — Obsidian-like 3D vector knowledge graph with search */

let _memoryScene = null;
let _memoryAnimId = null;

async function renderMemoryView(area) {
  showLoading(area);

  if (_memoryAnimId) { cancelAnimationFrame(_memoryAnimId); _memoryAnimId = null; }

  const res = await api('/api/memory/graph');
  const nodes = (res && res.nodes) || [];
  const edges = (res && res.edges) || [];

  area.innerHTML = `
    <div class="view-header">
      <h2><span>Memory</span> · Knowledge Graph</h2>
      <span class="muted">${nodes.length} node${nodes.length !== 1 ? 's' : ''} · ${edges.length} edge${edges.length !== 1 ? 's' : ''}</span>
    </div>
    <div style="position:relative;margin-bottom:12px">
      <input id="memory-search" type="text" placeholder="Search memories..." autocomplete="off"
        style="width:100%;padding:10px 14px;border-radius:6px;border:1px solid var(--color-glass-edge);background:var(--color-glass);color:var(--color-text);font-size:14px;outline:none;box-sizing:border-box"
        oninput="filterMemoryGraph(this.value)">
    </div>
    <div id="memory-graph-container" style="width:100%;height:560px;border-radius:8px;overflow:hidden;border:1px solid var(--color-glass-edge);background:var(--color-bg, #0A0E1A);position:relative">
      <div id="memory-graph-info" style="position:absolute;bottom:10px;left:10px;z-index:10;font-size:11px;color:var(--color-text-dim);pointer-events:none">
        Drag to rotate · Scroll to zoom · Click a node
      </div>
    </div>
    <div id="memory-node-detail" style="margin-top:10px;min-height:0"></div>
  `;

  if (!nodes.length) {
    document.getElementById('memory-search').style.display = 'none';
    document.getElementById('memory-graph-container').innerHTML = `
      <div class="empty-state" style="padding:60px 20px;text-align:center">
        <div class="icon" style="font-size:32px">◉</div>
        <h3>No memories yet</h3>
        <p style="color:var(--color-text-dim)">Memories appear when agents process commands.</p>
      </div>
    `;
    return;
  }

  // Defer Three.js init to next frame so DOM is ready
  requestAnimationFrame(() => initMemoryGraph(nodes, edges));
}

function initMemoryGraph(nodes, edges) {
  const container = document.getElementById('memory-graph-container');
  if (!container || typeof THREE === 'undefined') return;

  const W = container.clientWidth || 800;
  const H = container.clientHeight || 560;

  // Scene
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0A0E1A);

  // Camera
  const camera = new THREE.PerspectiveCamera(45, W / H, 0.1, 1000);
  camera.position.set(20, 15, 25);
  camera.lookAt(0, 0, 0);

  // Renderer
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setSize(W, H);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  container.appendChild(renderer.domElement);

  // Controls
  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.rotateSpeed = 0.8;
  controls.zoomSpeed = 1.2;
  controls.target.set(0, 0, 0);

  // Lighting
  const ambient = new THREE.AmbientLight(0x404060, 0.6);
  scene.add(ambient);
  const dir = new THREE.DirectionalLight(0xffffff, 0.8);
  dir.position.set(10, 20, 10);
  scene.add(dir);
  const back = new THREE.DirectionalLight(0x4488ff, 0.3);
  back.position.set(-10, -5, -10);
  scene.add(back);

  // Build force layout positions
  const pos = forceLayout3D(nodes, edges);

  // Create node meshes
  const nodeObjs = [];
  const colorMap = {};
  const sphereGeo = new THREE.SphereGeometry(0.6, 16, 16);

  nodes.forEach((n, i) => {
    const p = pos[i] || { x: 0, y: 0, z: 0 };
    const col = new THREE.Color(n.color || '#8899AA');
    colorMap[n.id] = col;
    const mat = new THREE.MeshStandardMaterial({
      color: col,
      emissive: col,
      emissiveIntensity: 0.15,
      roughness: 0.4,
      metalness: 0.3,
    });
    const mesh = new THREE.Mesh(sphereGeo, mat);
    mesh.position.set(p.x, p.y, p.z);
    mesh.userData = { node: n, scale: 1 };
    scene.add(mesh);
    nodeObjs.push(mesh);

    // Label sprite
    const label = makeLabel(n.label || n.id, col);
    label.position.set(p.x, p.y - 1.2, p.z);
    scene.add(label);
    mesh.userData.label = label;
  });

  // Create edge lines
  const edgeObjs = [];
  edges.forEach(e => {
    const srcNode = nodes.find(n => n.id === e.source);
    const tgtNode = nodes.find(n => n.id === e.target);
    if (!srcNode || !tgtNode) return;
    const srcIdx = nodes.indexOf(srcNode);
    const tgtIdx = nodes.indexOf(tgtNode);
    if (srcIdx < 0 || tgtIdx < 0) return;
    const p1 = pos[srcIdx];
    const p2 = pos[tgtIdx];
    if (!p1 || !p2) return;
    const points = [
      new THREE.Vector3(p1.x, p1.y, p1.z),
      new THREE.Vector3(p2.x, p2.y, p2.z),
    ];
    const geo = new THREE.BufferGeometry().setFromPoints(points);
    const mat = new THREE.LineBasicMaterial({
      color: 0x1B2D45,
      transparent: true,
      opacity: 0.4,
    });
    const line = new THREE.Line(geo, mat);
    scene.add(line);
    edgeObjs.push(line);
  });

  // Selection
  const raycaster = new THREE.Raycaster();
  const mouse = new THREE.Vector2();
  let selectedId = null;

  function onNodeClick(event) {
    const rect = renderer.domElement.getBoundingClientRect();
    mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(mouse, camera);
    const intersects = raycaster.intersectObjects(nodeObjs);
    if (intersects.length > 0) {
      const obj = intersects[0].object;
      const n = obj.userData.node;
      if (n) showMemoryNodeDetail(n);
    }
  }
  renderer.domElement.addEventListener('click', onNodeClick);

  // Animation loop
  let time = 0;
  function animate() {
    _memoryAnimId = requestAnimationFrame(animate);
    time += 0.005;
    // Gentle floating
    nodeObjs.forEach((obj, i) => {
      const n = obj.userData.node;
      const p = pos[i];
      if (p) {
        obj.position.x = p.x + Math.sin(time + i) * 0.1;
        obj.position.y = p.y + Math.cos(time * 0.7 + i) * 0.1;
        obj.position.z = p.z + Math.sin(time * 0.5 + i * 0.3) * 0.1;
        if (obj.userData.label) {
          obj.userData.label.position.copy(obj.position);
          obj.userData.label.position.y -= 1.2;
        }
      }
    });
    controls.update();
    renderer.render(scene, camera);
  }
  animate();

  // Resize handler
  function onResize() {
    const w = container.clientWidth;
    const h = container.clientHeight;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  }
  window.addEventListener('resize', onResize);

  // Store for cleanup
  _memoryScene = { scene, camera, renderer, controls, nodeObjs, edgeObjs, onResize };
}

function makeLabel(text, color) {
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  canvas.width = 512;
  canvas.height = 128;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.font = 'bold 28px Inter, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillStyle = 'rgba(10,14,26,0.7)';
  const tw = ctx.measureText(text).width;
  ctx.roundRect ? ctx.roundRect((512 - tw - 40) / 2, 20, tw + 40, 88, 8) : null;
  ctx.fill();
  ctx.fillStyle = '#' + color.getHexString();
  ctx.font = 'bold 28px Inter, sans-serif';
  ctx.fillText(text.length > 30 ? text.substring(0, 27) + '...' : text, 256, 64);
  const tex = new THREE.CanvasTexture(canvas);
  tex.needsUpdate = true;
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false });
  const sprite = new THREE.Sprite(mat);
  sprite.scale.set(6, 1.5, 1);
  return sprite;
}

function forceLayout3D(nodes, edges) {
  const n = nodes.length;
  if (!n) return [];
  const pos = nodes.map(() => ({
    x: (Math.random() - 0.5) * 15,
    y: (Math.random() - 0.5) * 15,
    z: (Math.random() - 0.5) * 15,
  }));
  const vel = nodes.map(() => ({ x: 0, y: 0, z: 0 }));
  const adj = {};
  edges.forEach(e => {
    if (!adj[e.source]) adj[e.source] = [];
    if (!adj[e.target]) adj[e.target] = [];
    adj[e.source].push(e.target);
    adj[e.target].push(e.source);
  });

  const iterations = 80;
  for (let iter = 0; iter < iterations; iter++) {
    const cooling = 1 - iter / iterations;
    // Repulsion
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const dx = pos[j].x - pos[i].x;
        const dy = pos[j].y - pos[i].y;
        const dz = pos[j].z - pos[i].z;
        const dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 0.1;
        const force = 30 / (dist * dist) * cooling;
        const fx = dx / dist * force;
        const fy = dy / dist * force;
        const fz = dz / dist * force;
        vel[i].x -= fx; vel[i].y -= fy; vel[i].z -= fz;
        vel[j].x += fx; vel[j].y += fy; vel[j].z += fz;
      }
    }
    // Attraction along edges
    edges.forEach(e => {
      const si = nodes.findIndex(n => n.id === e.source);
      const ti = nodes.findIndex(n => n.id === e.target);
      if (si < 0 || ti < 0) return;
      const dx = pos[ti].x - pos[si].x;
      const dy = pos[ti].y - pos[si].y;
      const dz = pos[ti].z - pos[si].z;
      const dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 0.1;
      const force = dist * 0.02 * cooling;
      const fx = dx / dist * force;
      const fy = dy / dist * force;
      const fz = dz / dist * force;
      vel[si].x += fx; vel[si].y += fy; vel[si].z += fz;
      vel[ti].x -= fx; vel[ti].y -= fy; vel[ti].z -= fz;
    });
    // Center gravity
    for (let i = 0; i < n; i++) {
      vel[i].x -= pos[i].x * 0.01 * cooling;
      vel[i].y -= pos[i].y * 0.01 * cooling;
      vel[i].z -= pos[i].z * 0.01 * cooling;
    }
    // Apply velocity with damping
    for (let i = 0; i < n; i++) {
      vel[i].x *= 0.85; vel[i].y *= 0.85; vel[i].z *= 0.85;
      pos[i].x += vel[i].x;
      pos[i].y += vel[i].y;
      pos[i].z += vel[i].z;
    }
  }
  return pos;
}

function filterMemoryGraph(query) {
  if (!_memoryScene) return;
  const q = query.toLowerCase().trim();
  _memoryScene.nodeObjs.forEach(obj => {
    const n = obj.userData.node;
    const match = !q || (n.label && n.label.toLowerCase().includes(q))
      || (n.agent && n.agent.toLowerCase().includes(q))
      || (n.command && n.command.toLowerCase().includes(q));
    obj.visible = match;
    if (obj.userData.label) obj.userData.label.visible = match;
    // Highlight matches
    if (match && q) {
      obj.material.emissiveIntensity = 0.5;
    } else {
      obj.material.emissiveIntensity = 0.15;
      obj.material.opacity = match ? 1 : 0.15;
      obj.material.transparent = !match;
    }
  });
  // Dim edges connected to hidden nodes
  _memoryScene.edgeObjs.forEach(line => {
    line.visible = true;
    line.material.opacity = 0.1;
  });
}

function showMemoryNodeDetail(node) {
  const el = document.getElementById('memory-node-detail');
  if (!el) return;
  el.innerHTML = `
    <div class="section glass">
      <div class="panel-title" style="display:flex;align-items:center;gap:8px">
        <span style="color:${escapeHtml(node.color || '#8899AA')}">●</span>
        ${escapeHtml(node.agent || 'unknown')}
        <span class="badge" style="font-size:10px">${escapeHtml(node.type || '')}</span>
      </div>
      <div style="padding:0 14px 14px">
        <p style="margin:0 0 6px;font-size:13px;line-height:1.5">${escapeHtml(node.label || '')}</p>
        <div style="display:flex;gap:12px;font-size:11px;color:var(--color-text-dim)">
          <span class="mono">${escapeHtml(node.timestamp || '')}</span>
          ${node.command ? `<code class="mono" style="background:var(--color-glass);padding:2px 8px;border-radius:4px">${escapeHtml(node.command)}</code>` : ''}
        </div>
      </div>
    </div>
  `;
}
