// Frontend logic (mobile-safe picker; no settings UI)
const sessionId = (crypto && crypto.randomUUID) ? crypto.randomUUID() : (Math.random().toString(36).slice(2));
let items = [];
let socket;
let users = [];
let albums = [];

// Status precedence: never regress (e.g., uploading -> done shouldn't go back to uploading)
const STATUS_ORDER = { queued: 0, checking: 1, uploading: 2, duplicate: 3, done: 3, error: 4 };
const FINAL_STATES = new Set(['done','duplicate','error']);

// --- Dark mode ---
function initDarkMode() {
  const stored = localStorage.getItem('theme');
  if (stored === 'dark' || (!stored && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
    document.documentElement.classList.add('dark');
  }
  updateThemeIcon();
}

function toggleDarkMode() {
  const isDark = document.documentElement.classList.toggle('dark');
  localStorage.setItem('theme', isDark ? 'dark' : 'light');
  updateThemeIcon();
}

function updateThemeIcon() {
  const isDark = document.documentElement.classList.contains('dark');
  document.getElementById('iconLight').classList.toggle('hidden', !isDark);
  document.getElementById('iconDark').classList.toggle('hidden', isDark);
}

initDarkMode();

// --- User and Album Management ---
async function loadUsers() {
  try {
    const response = await fetch('/api/users');
    if (!response.ok) throw new Error('Failed to load users');
    
    const data = await response.json();
    users = data.users || [];
    
    const userSelect = document.getElementById('userSelect');
    userSelect.innerHTML = '<option value="">Selecione um usuário</option>';
    
    users.forEach(user => {
      const option = document.createElement('option');
      option.value = user.id;
      option.textContent = user.name || user.email;
      userSelect.appendChild(option);
    });
    
    // Auto-select first user if only one exists
    if (users.length === 1) {
      userSelect.value = users[0].id;
      await loadAlbums(users[0].id);
    }
  } catch (error) {
    console.error('Error loading users:', error);
    document.getElementById('userSelect').innerHTML = '<option value="">Erro ao carregar usuários</option>';
  }
}

async function loadAlbums(userId) {
  if (!userId) {
    const albumSelect = document.getElementById('albumSelect');
    albumSelect.innerHTML = '<option value="">Selecione um usuário primeiro</option>';
    return;
  }
  
  try {
    const response = await fetch(`/api/albums?userId=${userId}`);
    if (!response.ok) throw new Error('Failed to load albums');
    
    const data = await response.json();
    albums = data.albums || [];
    
    const albumSelect = document.getElementById('albumSelect');
    albumSelect.innerHTML = '<option value="">Criar novo álbum</option>';
    
    albums.forEach(album => {
      const option = document.createElement('option');
      option.value = album.id;
      option.textContent = album.albumName;
      albumSelect.appendChild(option);
    });
  } catch (error) {
    console.error('Error loading albums:', error);
    document.getElementById('albumSelect').innerHTML = '<option value="">Erro ao carregar álbuns</option>';
  }
}

async function createAlbum(userId, albumName) {
  try {
    const response = await fetch('/api/albums', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        userId: userId,
        albumName: albumName
      })
    });
    
    if (!response.ok) throw new Error('Failed to create album');
    
    const data = await response.json();
    await loadAlbums(userId); // Reload albums
    
    // Select the newly created album
    document.getElementById('albumSelect').value = data.album.id;
    
    showBanner(`Álbum "${albumName}" criado com sucesso!`, 'ok');
    return data.album;
  } catch (error) {
    console.error('Error creating album:', error);
    showBanner('Erro ao criar álbum', 'error');
    throw error;
  }
}

function getSelectedUserAndAlbum() {
  const userId = document.getElementById('userSelect').value;
  const albumId = document.getElementById('albumSelect').value;
  
  if (!userId) {
    showBanner('Por favor, selecione um usuário', 'warn');
    return null;
  }
  
  return { userId, albumId };
}

// --- helpers ---
function human(bytes){
  if (!bytes) return '0 B';
  const k = 1024, sizes = ['B','KB','MB','GB','TB'];
  const i = Math.floor(Math.log(bytes)/Math.log(k));
  return (bytes/Math.pow(k,i)).toFixed(1)+' '+sizes[i];
}

function addItem(file){
  const selection = getSelectedUserAndAlbum();
  if (!selection) return;
  
  const id = (crypto && crypto.randomUUID) ? crypto.randomUUID() : (Math.random().toString(36).slice(2));
  const it = { 
    id, 
    file, 
    name: file.name, 
    size: file.size, 
    status: 'queued', 
    progress: 0,
    userId: selection.userId,
    albumId: selection.albumId
  };
  items.unshift(it);
  render();
}

function render(){
  const itemsEl = document.getElementById('items');
  itemsEl.innerHTML = items.map(it => `
    <div class="rounded-2xl border bg-white dark:bg-gray-800 dark:border-gray-700 p-4 shadow-sm transition-colors">
      <div class="flex items-center justify-between">
        <div class="min-w-0">
          <div class="truncate font-medium">${it.name} <span class="text-xs text-gray-500 dark:text-gray-400">(${human(it.size)})</span></div>
          <div class="mt-1 text-xs text-gray-600 dark:text-gray-400">
            ${it.message ? `<span>${it.message}</span>` : ''}
          </div>
        </div>
        <div class="text-sm">${it.status}</div>
      </div>
      <div class="mt-3 h-2 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-gray-700">
        <div class="h-full ${it.status==='done'?'bg-green-500':it.status==='duplicate'?'bg-amber-500':it.status==='error'?'bg-red-500':'bg-blue-500'}" style="width:${Math.max(it.progress, (it.status==='done'||it.status==='duplicate'||it.status==='error')?100:it.progress)}%"></div>
      </div>
      <div class="mt-2 text-sm text-gray-600 dark:text-gray-400">
        ${it.status==='uploading' ? `Uploadingâ€¦ ${it.progress}%` : it.status.charAt(0).toUpperCase()+it.status.slice(1)}
      </div>
    </div>
  `).join('');

  const c = {queued:0,uploading:0,done:0,dup:0,err:0};
  for(const it of items){
    if(['queued','checking'].includes(it.status)) c.queued++;
    if(it.status==='uploading') c.uploading++;
    if(it.status==='done') c.done++;
    if(it.status==='duplicate') c.dup++;
    if(it.status==='error') c.err++;
  }
  document.getElementById('countQueued').textContent=c.queued;
  document.getElementById('countUploading').textContent=c.uploading;
  document.getElementById('countDone').textContent=c.done;
  document.getElementById('countDup').textContent=c.dup;
  document.getElementById('countErr').textContent=c.err;
}

// --- WebSocket progress ---
function openSocket(){
  socket = new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws');
  socket.onopen = () => { socket.send(JSON.stringify({session_id: sessionId})); };
  socket.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    const { item_id, status, progress, message } = msg;
    const it = items.find(x => x.id===item_id);
    if(!it) return;
    // If we've already finalized this item, ignore late/regressive updates
    if (FINAL_STATES.has(it.status)) return;

    const cur = STATUS_ORDER[it.status] ?? 0;
    const inc = STATUS_ORDER[status] ?? 0;
    if (inc < cur) {
      // ignore regressive status updates
    } else {
      it.status = status;
    }
    if (typeof progress==='number') {
      // never decrease progress
      it.progress = Math.max(it.progress || 0, progress);
    }
    if (message) it.message = message;
    if (FINAL_STATES.has(it.status)) {
      it.progress = 100;
    }
    render();
  };
  socket.onclose = () => setTimeout(openSocket, 2000);
}
openSocket();

// --- Upload queue ---
async function runQueue(){
  let inflight = 0;
  async function runNext(){
    if(inflight >= 3) return; // client-side throttle; server handles uploads regardless
    const next = items.find(i => i.status==='queued');
    if(!next) return;
    next.status='checking';
    render();
    inflight++;
    try{
      const form = new FormData();
      form.append('file', next.file);
      form.append('item_id', next.id);
      form.append('session_id', sessionId);
      form.append('last_modified', next.file.lastModified || '');
      form.append('user_id', next.userId || '');
      form.append('album_id', next.albumId || '');
      
      const res = await fetch('/api/upload', { method:'POST', body: form });
      const body = await res.json().catch(()=>({}));
      if(!res.ok && next.status!=='error'){
        next.status='error';
        next.message = body.error || 'Upload failed';
        render();
      } else if (res.ok) {
        // Fallback finalize on HTTP success in case WS final message is missed
        const statusText = (body && body.status) ? String(body.status) : '';
        const isDuplicate = /duplicate/i.test(statusText);
        next.status = isDuplicate ? 'duplicate' : 'done';
        next.message = statusText || (isDuplicate ? 'Duplicate' : 'Uploaded');
        next.progress = 100;
        render();
        try { showBanner(isDuplicate ? `Duplicate: ${next.name}` : `Uploaded: ${next.name}`, isDuplicate ? 'warn' : 'ok'); } catch {}
      }
    }catch(err){
      next.status='error';
      next.message = String(err);
      render();
    }finally{
      inflight--;
      setTimeout(runNext, 50);
    }
  }
  for(let i=0;i<3;i++) runNext();
}

// --- DOM refs ---
const dz = document.getElementById('dropzone');
const fi = document.getElementById('fileInput');
const btnClearFinished = document.getElementById('btnClearFinished');
const btnClearAll = document.getElementById('btnClearAll');
const btnPing = document.getElementById('btnPing');
const pingStatus = document.getElementById('pingStatus');
const banner = document.getElementById('topBanner');
const btnTheme = document.getElementById('btnTheme');

// User/Album controls
const userSelect = document.getElementById('userSelect');
const albumSelect = document.getElementById('albumSelect');
const btnCreateAlbum = document.getElementById('btnCreateAlbum');
const newAlbumDiv = document.getElementById('newAlbumDiv');
const newAlbumName = document.getElementById('newAlbumName');
const btnConfirmAlbum = document.getElementById('btnConfirmAlbum');
const btnCancelAlbum = document.getElementById('btnCancelAlbum');

// --- Simple banner helper ---
function showBanner(text, kind='ok'){
  if(!banner) return;
  banner.textContent = text;
  // reset classes and apply based on kind
  banner.className = 'rounded-2xl p-3 text-center transition-colors ' + (
    kind==='ok' ? 'border border-green-200 bg-green-50 text-green-700 dark:bg-green-900 dark:border-green-700 dark:text-green-300'
    : kind==='warn' ? 'border border-amber-200 bg-amber-50 text-amber-700 dark:bg-amber-900 dark:border-amber-700 dark:text-amber-300'
    : 'border border-red-200 bg-red-50 text-red-700 dark:bg-red-900 dark:border-red-700 dark:text-red-300'
  );
  banner.classList.remove('hidden');
  setTimeout(() => banner.classList.add('hidden'), 3000);
}

// --- Event Handlers ---
userSelect.onchange = async () => {
  const userId = userSelect.value;
  if (userId) {
    await loadAlbums(userId);
  } else {
    albumSelect.innerHTML = '<option value="">Selecione um usuário primeiro</option>';
  }
};

btnCreateAlbum.onclick = () => {
  const userId = userSelect.value;
  if (!userId) {
    showBanner('Selecione um usuário primeiro', 'warn');
    return;
  }
  newAlbumDiv.classList.remove('hidden');
  newAlbumName.focus();
};

btnConfirmAlbum.onclick = async () => {
  const userId = userSelect.value;
  const albumName = newAlbumName.value.trim();
  
  if (!albumName) {
    showBanner('Digite o nome do álbum', 'warn');
    return;
  }
  
  try {
    await createAlbum(userId, albumName);
    newAlbumDiv.classList.add('hidden');
    newAlbumName.value = '';
  } catch (error) {
    // Error already handled in createAlbum
  }
};

btnCancelAlbum.onclick = () => {
  newAlbumDiv.classList.add('hidden');
  newAlbumName.value = '';
};

// Allow Enter key to confirm album creation
newAlbumName.onkeypress = (e) => {
  if (e.key === 'Enter') {
    btnConfirmAlbum.click();
  }
};

// --- Connection test with ephemeral banner ---
btnPing.onclick = async () => {
  pingStatus.textContent = 'checkingâ€¦';
  try{
    const r = await fetch('/api/ping', { method:'POST' });
    const j = await r.json();
    pingStatus.textContent = j.ok ? 'Connected' : 'No connection';
    pingStatus.className = 'ml-2 text-sm ' + (j.ok ? 'text-green-600' : 'text-red-600');
    if(j.ok){
      let bannerText = `Connected to Immich at ${j.base_url}`;
      if(j.album_name) {
        bannerText += ` | Uploading to album: "${j.album_name}"`;
      }
      showBanner(bannerText, 'ok');
    }
  }catch{
    pingStatus.textContent = 'No connection';
    pingStatus.className='ml-2 text-sm text-red-600';
  }
};

// --- Drag & drop (no click-to-open on touch) ---
['dragenter','dragover'].forEach(ev => dz.addEventListener(ev, e=>{ e.preventDefault(); dz.classList.add('border-blue-500','bg-blue-50','dark:bg-blue-900','dark:bg-opacity-20'); }));
['dragleave','drop'].forEach(ev => dz.addEventListener(ev, e=>{ e.preventDefault(); dz.classList.remove('border-blue-500','bg-blue-50','dark:bg-blue-900','dark:bg-opacity-20'); }));
dz.addEventListener('drop', (e)=>{
  e.preventDefault();
  const files = Array.from(e.dataTransfer.files || []);
  const accepted = files.filter(f => /^(image|video)\//.test(f.type) || /\.(jpe?g|png|heic|heif|webp|gif|tiff|bmp|mp4|mov|m4v|avi|mkv)$/i.test(f.name));
  accepted.forEach(addItem);
  render();
  runQueue();
});

// --- Mobile-safe file input change handler ---
const isTouch = ('ontouchstart' in window) || (navigator.maxTouchPoints > 0);
let suppressClicksUntil = 0;

fi.addEventListener('click', (e) => {
  // prevent bubbling to parents (extra safety)
  e.stopPropagation();
});

fi.onchange = () => {
  // Suppress any stray clicks for a short window after the picker closes
  suppressClicksUntil = Date.now() + 800;

  const files = Array.from(fi.files || []);
  const accepted = files.filter(f =>
    /^(image|video)\//.test(f.type) ||
    /\.(jpe?g|png|heic|heif|webp|gif|tiff|bmp|mp4|mov|m4v|avi|mkv)$/i.test(f.name)
  );
  accepted.forEach(addItem);
  render();
  runQueue();

  // Reset a bit later so selecting the same items again still triggers 'change'
  setTimeout(() => { try { fi.value = ''; } catch {} }, 500);
};

// If you want the whole dropzone clickable on desktop only, enable this:
if (!isTouch) {
  dz.addEventListener('click', () => {
    // avoid accidental double-open if something weird happens
    if (Date.now() < suppressClicksUntil) return;
    try { fi.value = ''; } catch {}
    fi.click();
  });
}

// --- Clear buttons ---
btnClearFinished.onclick = ()=>{ items = items.filter(i => !['done','duplicate'].includes(i.status)); render(); };
btnClearAll.onclick = ()=>{ items = []; render(); };

// --- Dark mode toggle ---
btnTheme.onclick = toggleDarkMode;

// --- Initialize ---
window.addEventListener('DOMContentLoaded', async () => {
  await loadUsers();
});