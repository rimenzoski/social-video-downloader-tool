/**
 * Front-end do Benchmark Video Archiver.
 *
 * Fluxo:
 *   1. "Processar Fila" baixa todos os videos sequencialmente.
 *   2. Quando o lote termina, abre um modal pedindo a pasta de destino.
 *   3. Um unico dialogo salva todos os videos concluidos no mesmo lugar.
 */

const IDLE_POLL_MS = 2500;
const BUSY_POLL_MS = 700;

const STATUS_STYLES = {
  pending:     { label: 'Aguardando', pill: 'bg-slate-100 text-slate-600',   bar: 'bg-slate-300',   dot: 'bg-slate-400' },
  queued:      { label: 'Na fila',    pill: 'bg-indigo-50 text-indigo-700',  bar: 'bg-indigo-400',  dot: 'bg-indigo-500' },
  downloading: { label: 'Baixando',   pill: 'bg-blue-50 text-blue-700',      bar: 'bg-blue-500',    dot: 'bg-blue-500' },
  processing:  { label: 'Convertendo',pill: 'bg-amber-50 text-amber-700',    bar: 'bg-amber-500',   dot: 'bg-amber-500' },
  done:        { label: 'Concluído',  pill: 'bg-emerald-50 text-emerald-700',bar: 'bg-emerald-500', dot: 'bg-emerald-500' },
  error:       { label: 'Erro',       pill: 'bg-rose-50 text-rose-700',      bar: 'bg-rose-500',    dot: 'bg-rose-500' },
};

const el = (id) => document.getElementById(id);

const dom = {
  input: el('links-input'),
  counter: el('link-counter'),
  load: el('btn-load'),
  process: el('btn-process'),
  saveReady: el('btn-save-ready'),
  clearDone: el('btn-clear-done'),
  clearAll: el('btn-clear-all'),
  toast: el('toast'),
  grid: el('queue-grid'),
  empty: el('empty-state'),
  hint: el('queue-hint'),
  ffmpeg: el('ffmpeg-badge'),
  stats: {
    total: el('stat-total'),
    busy: el('stat-busy'),
    done: el('stat-done'),
    error: el('stat-error'),
  },
  overallBar: el('overall-bar'),
  overallLabel: el('overall-label'),
};

const cards = new Map();
let pollTimer = null;
let toastTimer = null;
let latestJobs = [];
let processingLock = false;

/** True enquanto aguardamos o lote iniciado por "Processar Fila" terminar. */
let batchInProgress = false;
/** Evita abrir o modal automaticamente mais de uma vez por lote. */
let autoSaveModalTriggered = false;
let savingBatch = false;

const savedJobIds = new Set();

/**
 * O modal e construido aqui, e nao no template, para que a interface continue
 * funcionando mesmo se o navegador servir uma versao antiga do HTML em cache.
 */
function buildSaveModal() {
  const overlay = document.createElement('div');
  overlay.id = 'save-modal';
  overlay.className =
    'hidden fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-6 backdrop-blur-sm';
  overlay.innerHTML = `
    <div class="bento w-full max-w-md p-6">
      <h3 class="text-base font-semibold text-slate-900">Downloads concluídos</h3>
      <p data-role="text" class="mt-2 text-sm leading-relaxed text-slate-600"></p>
      <p data-role="status" class="mt-3 hidden rounded-lg px-3 py-2 text-xs leading-relaxed"></p>
      <div class="mt-5 flex flex-wrap gap-3">
        <button data-role="pick"
          class="rounded-xl bg-slate-900 px-4 py-2.5 text-sm font-medium text-white transition hover:bg-slate-800 disabled:bg-slate-300">
          Salvar vídeos
        </button>
        <button data-role="skip"
          class="rounded-xl border border-slate-200 px-4 py-2.5 text-sm font-medium text-slate-600 transition hover:bg-slate-50">
          Agora não
        </button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  return {
    root: overlay,
    text: overlay.querySelector('[data-role="text"]'),
    status: overlay.querySelector('[data-role="status"]'),
    pick: overlay.querySelector('[data-role="pick"]'),
    skip: overlay.querySelector('[data-role="skip"]'),
  };
}

const modal = buildSaveModal();

// --------------------------------------------------------------------------
// Utilidades
// --------------------------------------------------------------------------

async function api(path, body) {
  const options = body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : { method: 'GET' };
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

/**
 * Fica acima do modal (z-50) para nao sumir atras do overlay. A centralizacao usa
 * `mx-auto w-max` porque o translate de `left-1/2` seria sobrescrito pelo
 * transform da animacao fade-up.
 */
const TOAST_BASE =
  'animate-fade-up fixed inset-x-0 bottom-6 z-[60] mx-auto w-max max-w-[90vw] rounded-xl ' +
  'px-4 py-2.5 text-sm font-medium shadow-lg shadow-slate-900/15';

function toast(message, tone = 'dark') {
  const palette = {
    error: 'bg-rose-600 text-white',
    success: 'bg-emerald-600 text-white',
    dark: 'bg-slate-900 text-white',
  };
  clearTimeout(toastTimer);
  dom.toast.textContent = message;
  dom.toast.className = `${TOAST_BASE} ${palette[tone] || palette.dark}`;
  toastTimer = setTimeout(() => dom.toast.classList.add('hidden'), 5000);
}

function prettyUrl(url) {
  try {
    const { hostname, pathname } = new URL(url);
    return hostname.replace(/^www\./, '') + pathname;
  } catch {
    return url;
  }
}

function formatDuration(seconds) {
  if (!seconds && seconds !== 0) return null;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

function unsavedCompletedJobs(jobs = latestJobs) {
  return jobs.filter(
    (job) => job.status === 'done' && job.filename && !savedJobIds.has(job.id)
  );
}

// --------------------------------------------------------------------------
// Processamento
// --------------------------------------------------------------------------

async function startProcessing() {
  if (processingLock) return false;

  const pending = latestJobs.filter((job) => job.status === 'pending');
  if (pending.length === 0) {
    toast('Nenhum item pendente na fila.');
    return false;
  }

  processingLock = true;
  dom.process.disabled = true;

  try {
    const result = await api('/api/process', {});
    if (result.started > 0) {
      batchInProgress = true;
      autoSaveModalTriggered = false;
      toast(`Processando ${result.started} item(ns), um por vez.`);
      refresh();
      return true;
    }
    toast('Nenhum item pendente na fila.');
    return false;
  } catch (error) {
    toast(`Erro ao iniciar a fila: ${error.message}`, 'error');
    return false;
  } finally {
    processingLock = false;
  }
}

// --------------------------------------------------------------------------
// Salvamento pos-lote
// --------------------------------------------------------------------------

/** Mensagem dentro do modal — o toast da pagina fica atras do overlay. */
function modalStatus(message, tone = 'info') {
  if (!message) {
    modal.status.classList.add('hidden');
    return;
  }
  const palette = {
    info: 'bg-slate-100 text-slate-600',
    error: 'bg-rose-50 text-rose-700',
    success: 'bg-emerald-50 text-emerald-700',
  };
  modal.status.className = `mt-3 rounded-lg px-3 py-2 text-xs leading-relaxed ${palette[tone]}`;
  modal.status.textContent = message;
}

function showSaveModal(count) {
  modal.text.textContent =
    count === 1
      ? '1 vídeo pronto para ser salvo no seu computador.'
      : `${count} vídeos prontos. Todos vão para a mesma pasta.`;

  modalStatus(null);
  modal.pick.disabled = false;
  modal.pick.textContent = count === 1 ? 'Salvar vídeo' : `Salvar ${count} vídeos`;
  modal.root.classList.remove('hidden');
  modal.pick.focus();
}

function hideSaveModal() {
  modal.root.classList.add('hidden');
}

/**
 * Download nativo do navegador.
 *
 * Os arquivos sao entregues um a um pelo mecanismo padrao de download. A pasta
 * e definida pelo proprio navegador: seja a pasta de downloads configurada, seja
 * a que o usuario escolher no primeiro arquivo. Como a decisao pertence ao
 * navegador e nao a pagina, ela vale automaticamente para o lote inteiro.
 */
async function downloadBatch() {
  if (savingBatch) return;

  const jobs = unsavedCompletedJobs();
  if (!jobs.length) {
    hideSaveModal();
    return;
  }

  savingBatch = true;
  modal.pick.disabled = true;

  try {
    for (const [index, job] of jobs.entries()) {
      modal.pick.textContent = `Salvando ${index + 1}/${jobs.length}…`;

      const link = document.createElement('a');
      link.href = `/api/file/${job.id}`;
      link.download = job.filename;
      link.rel = 'noopener';
      document.body.appendChild(link);
      link.click();
      link.remove();

      savedJobIds.add(job.id);
      const card = cards.get(job.id);
      if (card) updateCard(card, job);

      // Intervalo entre downloads: disparos simultaneos sao descartados
      // por alguns navegadores.
      if (index < jobs.length - 1) {
        await new Promise((resolve) => setTimeout(resolve, 800));
      }
    }

    batchInProgress = false;
    render(latestJobs);
    hideSaveModal();
    toast(
      jobs.length === 1
        ? 'Vídeo salvo com sucesso.'
        : `${jobs.length} vídeos salvos com sucesso na mesma pasta.`,
      'success'
    );
  } catch (error) {
    modalStatus(`Falha ao baixar: ${error.name || 'Erro'} — ${error.message}`, 'error');
  } finally {
    savingBatch = false;
    modal.pick.disabled = false;
    modal.pick.textContent = 'Salvar vídeos';
  }
}

/**
 * Decide quando pedir a pasta de destino.
 *
 * O estado de "lote rodando" e inferido do proprio polling, e nao apenas do
 * clique em "Processar Fila": assim o modal continua aparecendo mesmo se a
 * pagina for recarregada no meio dos downloads.
 */
function checkBatchComplete(counts) {
  if (counts.busy > 0) {
    batchInProgress = true;
    autoSaveModalTriggered = false;
    return;
  }

  if (!batchInProgress || autoSaveModalTriggered || savingBatch) return;

  batchInProgress = false;
  autoSaveModalTriggered = true;

  const toSave = unsavedCompletedJobs();
  if (toSave.length === 0) {
    toast('Lote concluído, mas nenhum vídeo foi baixado com sucesso.', 'error');
    return;
  }

  showSaveModal(toSave.length);
}

function detailLine(job) {
  if (job.status === 'error') return job.error || 'Falha no download';
  if (job.status === 'downloading') {
    const parts = [];
    if (job.speed) parts.push(`${job.speed}/s`);
    if (job.eta) parts.push(`faltam ${job.eta}s`);
    if (job.filesize) parts.push(job.filesize);
    return parts.join(' · ') || 'Baixando…';
  }
  if (job.status === 'done') {
    const parts = [];
    parts.push(savedJobIds.has(job.id) ? 'Salvo pelo navegador' : 'Pronto para salvar');
    const duration = formatDuration(job.duration);
    if (duration) parts.push(duration);
    if (job.filesize) parts.push(job.filesize);
    return parts.join(' · ');
  }
  return job.message || '';
}

// --------------------------------------------------------------------------
// Cards
// --------------------------------------------------------------------------

function createCard(job) {
  const node = document.createElement('article');
  node.className = 'bento animate-fade-up flex flex-col p-5';
  node.dataset.jobId = job.id;

  node.innerHTML = `
    <div class="mb-3 flex items-center justify-between gap-2">
      <div class="flex items-center gap-2 min-w-0">
        <span data-role="dot" class="h-1.5 w-1.5 shrink-0 rounded-full"></span>
        <span data-role="platform" class="truncate text-xs font-medium text-slate-500"></span>
      </div>
      <span data-role="pill" class="shrink-0 rounded-full px-2.5 py-1 text-[11px] font-medium"></span>
    </div>

    <div data-role="preview"
      class="group/preview relative mb-3 hidden aspect-video cursor-pointer overflow-hidden rounded-xl bg-slate-900">
      <video data-role="video" class="h-full w-full object-cover"
        muted loop playsinline preload="metadata"></video>
      <div data-role="overlay"
        class="pointer-events-none absolute inset-0 flex items-center justify-center bg-slate-900/25 transition-opacity duration-200 group-hover/preview:opacity-0">
        <span class="flex h-10 w-10 items-center justify-center rounded-full bg-white/90 shadow-sm">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" class="ml-0.5 text-slate-900">
            <path d="M8 5v14l11-7z" />
          </svg>
        </span>
      </div>
    </div>

    <p data-role="title" class="mb-1 line-clamp-2 text-sm font-medium leading-snug text-slate-900"></p>
    <p data-role="url" class="mb-4 truncate font-mono text-[11px] text-slate-400"></p>

    <div class="mt-auto">
      <div class="mb-1.5 flex items-center justify-between text-[11px]">
        <span data-role="message" class="truncate pr-2 text-slate-500"></span>
        <span data-role="percent" class="shrink-0 font-mono text-slate-900"></span>
      </div>
      <div class="h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
        <div data-role="bar" class="h-full w-0 rounded-full transition-all duration-300"></div>
      </div>
      <div class="mt-3 flex items-start justify-between gap-2">
        <p data-role="detail" class="min-w-0 flex-1 break-words text-[11px] leading-relaxed text-slate-400"></p>
        <button data-role="retry"
          class="hidden shrink-0 rounded-lg border border-slate-200 px-2 py-1 text-[11px] font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50">
          Tentar de novo
        </button>
      </div>

      <div data-role="location" class="mt-3 hidden rounded-lg bg-slate-50 p-2.5">
        <p class="text-[10px] font-medium uppercase tracking-wide text-slate-400">Arquivo em disco</p>
        <p data-role="path" class="mt-1 break-all font-mono text-[10px] leading-relaxed text-slate-500"></p>
        <button data-role="reveal"
          class="mt-2 inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-[11px] font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" />
          </svg>
          Abrir pasta
        </button>
      </div>
    </div>
  `;

  node.querySelector('[data-role="retry"]').addEventListener('click', async () => {
    try {
      await api(`/api/retry/${job.id}`, {});
      batchInProgress = true;
      autoSaveModalTriggered = false;
      refresh();
    } catch (error) {
      toast(`Nao foi possivel reprocessar: ${error.message}`, 'error');
    }
  });

  node.querySelector('[data-role="reveal"]').addEventListener('click', async () => {
    try {
      const result = await api(`/api/reveal/${job.id}`, {});
      toast(`Pasta aberta: ${result.opened}`);
    } catch (error) {
      toast(`Não foi possível abrir a pasta: ${error.message}`, 'error');
    }
  });

  const preview = node.querySelector('[data-role="preview"]');
  const video = node.querySelector('[data-role="video"]');

  preview.addEventListener('mouseenter', () => {
    // play() rejeita se o card sair da tela antes do buffer encher.
    video.play().catch(() => {});
  });
  preview.addEventListener('mouseleave', () => {
    video.pause();
    video.currentTime = 0;
  });

  dom.grid.appendChild(node);
  cards.set(job.id, node);
  return node;
}

function updateCard(node, job) {
  const style = STATUS_STYLES[job.status] || STATUS_STYLES.pending;
  const pick = (role) => node.querySelector(`[data-role="${role}"]`);

  pick('dot').className = `h-1.5 w-1.5 shrink-0 rounded-full ${style.dot}`;
  pick('platform').textContent = job.platform;

  const pill = pick('pill');
  pill.className = `shrink-0 rounded-full px-2.5 py-1 text-[11px] font-medium ${style.pill}`;
  pill.textContent = style.label;

  pick('title').textContent = job.title || prettyUrl(job.url);
  pick('url').textContent = job.url;
  pick('url').title = job.url;
  pick('message').textContent = job.message || '';
  pick('percent').textContent = `${Math.round(job.progress)}%`;

  const bar = pick('bar');
  const striped = job.status === 'downloading' || job.status === 'processing';
  bar.className = `h-full rounded-full transition-all duration-300 ${style.bar} ${striped ? 'bar-active' : ''}`;
  bar.style.width = `${job.status === 'done' ? 100 : job.progress}%`;

  const detail = pick('detail');
  detail.textContent = detailLine(job);
  detail.className = `min-w-0 flex-1 break-words text-[11px] leading-relaxed ${
    job.status === 'error' ? 'text-rose-600' : 'text-slate-400'
  }`;

  pick('retry').classList.toggle('hidden', job.status !== 'error');

  const isDone = job.status === 'done';
  const video = pick('video');
  // O src so e atribuido uma vez: reatribuir a cada poll reiniciaria o
  // carregamento e derrubaria o video no meio da reproducao.
  if (isDone && !video.src) {
    // O fragmento #t=0.1 forca o navegador a renderizar um quadro como capa.
    video.src = `/api/preview/${job.id}#t=0.1`;
  }
  pick('preview').classList.toggle('hidden', !isDone);

  pick('location').classList.toggle('hidden', !(isDone && job.filepath));
  if (isDone && job.filepath) {
    const path = pick('path');
    path.textContent = job.filepath;
    path.title = job.filepath;
  }
}

function render(jobs) {
  const seen = new Set();

  jobs.forEach((job) => {
    seen.add(job.id);
    updateCard(cards.get(job.id) || createCard(job), job);
  });

  cards.forEach((node, id) => {
    if (!seen.has(id)) {
      node.remove();
      cards.delete(id);
    }
  });

  const hasJobs = jobs.length > 0;
  dom.grid.classList.toggle('hidden', !hasJobs);
  dom.grid.classList.toggle('grid', hasJobs);
  dom.empty.classList.toggle('hidden', hasJobs);
}

function renderStats(counts, jobs) {
  dom.stats.total.textContent = counts.total;
  dom.stats.busy.textContent = counts.busy;
  dom.stats.done.textContent = counts.done;
  dom.stats.error.textContent = counts.error;

  const overall = jobs.length
    ? jobs.reduce((sum, job) => sum + (job.status === 'done' ? 100 : job.progress), 0) / jobs.length
    : 0;
  dom.overallBar.style.width = `${overall}%`;
  dom.overallLabel.textContent = `${Math.round(overall)}%`;

  dom.process.disabled = counts.pending === 0 || processingLock || counts.busy > 0;
  dom.process.textContent =
    counts.pending > 0 ? `Processar Fila (${counts.pending})` : 'Processar Fila';

  dom.hint.textContent = counts.busy > 0 ? `${counts.busy} em andamento` : '';

  const unsaved = unsavedCompletedJobs(jobs).length;
  if (dom.saveReady) {
    dom.saveReady.classList.toggle('hidden', unsaved === 0);
    dom.saveReady.textContent = unsaved > 1 ? `Salvar ${unsaved} vídeos` : 'Salvar vídeo';
  }
}

// --------------------------------------------------------------------------
// Polling
// --------------------------------------------------------------------------

async function refresh() {
  try {
    const { jobs, counts } = await api('/api/status');
    latestJobs = jobs;
    render(jobs);
    renderStats(counts, jobs);
    checkBatchComplete(counts);
    schedule(counts.busy > 0 ? BUSY_POLL_MS : IDLE_POLL_MS);
  } catch (error) {
    dom.hint.textContent = 'sem conexao com o servidor';
    schedule(IDLE_POLL_MS);
  }
}

function schedule(delay) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(refresh, delay);
}

// --------------------------------------------------------------------------
// Eventos
// --------------------------------------------------------------------------

function countLines() {
  const lines = dom.input.value.split('\n').filter((line) => line.trim());
  dom.counter.textContent = `${lines.length} ${lines.length === 1 ? 'linha' : 'linhas'}`;
}

dom.input.addEventListener('input', countLines);

dom.load.addEventListener('click', async () => {
  const raw = dom.input.value;
  if (!raw.trim()) {
    toast('Cole ao menos um link no campo acima.', 'error');
    return;
  }

  dom.load.disabled = true;
  try {
    const result = await api('/api/queue', { links: raw });
    const notes = [`${result.added} link(s) na fila`];
    if (result.duplicated) notes.push(`${result.duplicated} duplicado(s) ignorado(s)`);
    if (result.invalid) notes.push(`${result.invalid} invalido(s)`);
    toast(notes.join(' · '));

    if (result.added > 0) {
      dom.input.value = '';
      countLines();
    }
    refresh();
  } catch (error) {
    toast(`Erro ao carregar links: ${error.message}`, 'error');
  } finally {
    dom.load.disabled = false;
  }
});

dom.process.addEventListener('click', () => {
  startProcessing();
});

dom.clearDone.addEventListener('click', async () => {
  await api('/api/clear', { scope: 'done' });
  unsavedCompletedJobs().forEach((job) => savedJobIds.delete(job.id));
  refresh();
});

dom.clearAll.addEventListener('click', async () => {
  await api('/api/clear', { scope: 'all' });
  savedJobIds.clear();
  batchInProgress = false;
  autoSaveModalTriggered = false;
  hideSaveModal();
  refresh();
});

modal.pick.addEventListener('click', downloadBatch);

modal.skip.addEventListener('click', () => {
  hideSaveModal();
  const count = unsavedCompletedJobs().length;
  if (count > 0) {
    toast(`${count} vídeo(s) prontos. Use o botão "Salvar vídeos" quando quiser.`);
  }
});

dom.saveReady?.addEventListener('click', () => {
  const count = unsavedCompletedJobs().length;
  if (count > 0) showSaveModal(count);
});

dom.input.addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
    event.preventDefault();
    dom.load.click();
  }
});

// --------------------------------------------------------------------------
// Boot
// --------------------------------------------------------------------------

(async function init() {
  countLines();
  refresh();

  try {
    const health = await api('/api/health');
    dom.ffmpeg.textContent = health.ffmpeg ? 'ffmpeg pronto' : 'ffmpeg ausente';
    dom.ffmpeg.className = health.ffmpeg
      ? 'rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1.5 text-xs font-medium text-emerald-700'
      : 'rounded-full border border-rose-200 bg-rose-50 px-3 py-1.5 text-xs font-medium text-rose-700';
    if (!health.ffmpeg) toast(`ffmpeg nao encontrado. Rode: ${health.ffmpeg_hint}`, 'error');
  } catch {
    dom.ffmpeg.textContent = 'servidor offline';
  }
})();
