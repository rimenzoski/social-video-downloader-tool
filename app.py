"""
Social Video Downloader — ferramenta web local para arquivar videos de
Instagram, Facebook e Reddit em .mp4 para documentacao de benchmarks de design.

Arquitetura:
    Flask (HTTP) --> fila em memoria --> 1 unica thread worker --> yt-dlp/ffmpeg

O processamento e estritamente sequencial: existe apenas UMA thread worker
consumindo a fila, o que evita rajadas de requisicoes simultaneas para a mesma
plataforma (principal causa de bloqueio de IP / rate limit).
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request, send_file
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

# --------------------------------------------------------------------------- #
# Configuracao
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "benchmarks_videos"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Pausa entre downloads consecutivos. Um respiro entre requisicoes reduz muito
# a chance de rate limit em Instagram/Facebook.
DELAY_BETWEEN_DOWNLOADS = float(os.getenv("DOWNLOAD_DELAY", "3"))

# Conteudo privado / logado exige cookies. Defina UMA das duas variaveis:
#   COOKIES_FROM_BROWSER=chrome   (ou safari, firefox, brave, edge)
#   COOKIES_FILE=/caminho/cookies.txt
COOKIES_FROM_BROWSER = os.getenv("COOKIES_FROM_BROWSER", "").strip() or None
_cookies_file = os.getenv("COOKIES_FILE", "").strip()
COOKIES_FILE = str(Path(_cookies_file).expanduser()) if _cookies_file else None

MAX_TITLE_LEN = 60

HOST = "127.0.0.1"
PORT = int(os.getenv("PORT", "5001"))

# Dominios que o yt-dlp pode visitar. Sem essa lista qualquer URL http(s) seria
# aceita — inclusive enderecos da rede local ou de metadados de nuvem — e a
# ferramenta viraria um proxy de requisicoes as cegas (SSRF) para quem
# conseguisse fazer o usuario colar um link preparado.
ALLOWED_URL_HOSTS = {
    "instagram.com",
    "facebook.com",
    "fb.com",
    "fb.watch",
    "reddit.com",
    "redd.it",
}
# Escotilha para quem precisar de outra plataforma sem editar o codigo:
#   EXTRA_ALLOWED_HOSTS=vimeo.com,youtube.com python app.py
ALLOWED_URL_HOSTS |= {
    host.strip().lower().lstrip(".")
    for host in os.getenv("EXTRA_ALLOWED_HOSTS", "").split(",")
    if host.strip()
}

FFMPEG_INSTALL_HINT = (
    "brew install ffmpeg"
    if sys.platform == "darwin"
    else "winget install Gyan.FFmpeg"
    if sys.platform.startswith("win")
    else "sudo apt install ffmpeg"
)

STATUS_PENDING = "pending"
STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_ERROR = "error"

ACTIVE_STATUSES = {STATUS_QUEUED, STATUS_DOWNLOADING, STATUS_PROCESSING}

app = Flask(__name__)

# Ferramenta local: cache do navegador so atrapalha, pois deixa a interface
# rodando uma versao antiga do HTML/JS depois de cada ajuste no codigo.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
# Sem debug o Jinja mantem o template em memoria, entao mudancas no index.html
# so apareceriam depois de reiniciar o servidor.
app.config["TEMPLATES_AUTO_RELOAD"] = True
# A API so recebe listas de links; nao existe upload. Um teto baixo evita que um
# corpo gigante consuma memoria do processo.
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024

MAX_LINKS_PER_REQUEST = 200

# Hosts aceitos no cabecalho Host das requisicoes.
ALLOWED_REQUEST_HOSTS = {
    f"127.0.0.1:{PORT}",
    f"localhost:{PORT}",
    f"[::1]:{PORT}",
}

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        # 'unsafe-inline' e inevitavel: o tailwind.config vive num <script> inline
        # e o CDN gera <style> em tempo de execucao.
        "script-src 'self' https://cdn.tailwindcss.com 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "media-src 'self'",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    )
)


# --------------------------------------------------------------------------- #
# Seguranca
# --------------------------------------------------------------------------- #


@app.before_request
def _guard_local_requests():
    """
    Fecha os dois furos classicos de um servidor que escuta em localhost.

    1. DNS rebinding: um site externo aponta o proprio dominio para 127.0.0.1 e,
       como o navegador passa a considerar tudo "mesma origem", consegue LER as
       respostas da API (lista de jobs, caminhos em disco, os proprios videos).
       Conferir o Host recebido derruba esse vetor, porque o navegador envia o
       dominio do atacante, e nao 127.0.0.1.

    2. CSRF: um <form> em outro site dispara POST sem passar por preflight CORS.
       As rotas que mudam estado (/api/process, /api/clear, /api/reveal) nao
       dependem do corpo, entao seriam acionaveis por qualquer pagina aberta no
       navegador. Exigir origem propria resolve.
    """
    if request.host.lower() not in ALLOWED_REQUEST_HOSTS:
        return jsonify({"error": "Host nao autorizado"}), 403

    if request.method in SAFE_METHODS:
        return None

    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site is not None:
        if fetch_site != "same-origin":
            return jsonify({"error": "Origem nao autorizada"}), 403
        return None

    origin = request.headers.get("Origin")
    if origin is not None:
        if urlsplit(origin).netloc.lower() not in ALLOWED_REQUEST_HOSTS:
            return jsonify({"error": "Origem nao autorizada"}), 403
        return None

    # Navegadores atuais sempre mandam Sec-Fetch-Site ou Origin em POST. A
    # ausencia dos dois indica um cliente que nao e a interface do app.
    return jsonify({"error": "Origem ausente"}), 403


@app.after_request
def _harden_response(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY

    # O preview e a unica excecao ao no-store: sem cache o navegador rebaixaria
    # o video a cada hover, em vez de reaproveitar os trechos ja recebidos.
    if request.endpoint != "preview_file":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response

# --------------------------------------------------------------------------- #
# Estado compartilhado
# --------------------------------------------------------------------------- #

JOBS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
JOBS_LOCK = threading.RLock()
WORK_QUEUE: "Queue[str]" = Queue()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_job(url: str) -> Dict[str, Any]:
    job_id = uuid.uuid4().hex
    return {
        "id": job_id,
        "short_id": job_id[:8],
        "url": url,
        "status": STATUS_PENDING,
        "progress": 0.0,
        "message": "Aguardando na fila",
        "title": None,
        "platform": _guess_platform(url),
        "thumbnail": None,
        "duration": None,
        "filename": None,
        "filepath": None,
        "filesize": None,
        "speed": None,
        "eta": None,
        "error": None,
        "created_at": _now(),
        "finished_at": None,
    }


def _is_allowed_url(url: str) -> bool:
    """
    Aceita apenas http(s) em dominios da allowlist.

    A checagem e feita por sufixo de dominio (e nao por substring) para que
    `instagram.com.evil.net` seja recusado.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False

    if parsed.scheme.lower() not in ("http", "https"):
        return False

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False

    return any(
        host == allowed or host.endswith(f".{allowed}")
        for allowed in ALLOWED_URL_HOSTS
    )


def _guess_platform(url: str) -> str:
    host = url.lower()
    if "instagram" in host:
        return "Instagram"
    if "facebook" in host or "fb.watch" in host or "fb.com" in host:
        return "Facebook"
    if "reddit" in host or "redd.it" in host:
        return "Reddit"
    return "Outro"


def _update_job(job_id: str, **fields: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.update(fields)


def _get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def _snapshot() -> List[Dict[str, Any]]:
    with JOBS_LOCK:
        return [dict(job) for job in JOBS.values()]


# --------------------------------------------------------------------------- #
# yt-dlp
# --------------------------------------------------------------------------- #


class _SilentLogger:
    """Mantem o terminal limpo: o estado real de cada job vai para a UI."""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


def _human_size(num_bytes: Optional[float]) -> Optional[str]:
    if not num_bytes:
        return None
    units = ["B", "KB", "MB", "GB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return None


def _build_outtmpl(job: Dict[str, Any]) -> str:
    """Template de nome de arquivo com timestamp + id curto (evita sobreposicao)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return str(
        DOWNLOAD_DIR
        / f"{job['platform']}_%(title).{MAX_TITLE_LEN}s_{stamp}_{job['short_id']}.%(ext)s"
    )


def _ydl_options(job: Dict[str, Any]) -> Dict[str, Any]:
    def progress_hook(data: Dict[str, Any]) -> None:
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100) if total else 0.0
            _update_job(
                job["id"],
                status=STATUS_DOWNLOADING,
                progress=round(min(percent, 99.0), 1),
                message="Baixando",
                speed=_human_size(data.get("speed")),
                eta=data.get("eta"),
                filesize=_human_size(total),
            )
        elif status == "finished":
            _update_job(
                job["id"],
                status=STATUS_PROCESSING,
                progress=99.0,
                message="Convertendo para .mp4",
                speed=None,
                eta=None,
            )

    def postprocessor_hook(data: Dict[str, Any]) -> None:
        if data.get("status") == "started":
            name = data.get("postprocessor", "")
            label = (
                "Mesclando audio e video"
                if name in ("Merger", "FFmpegMerger")
                else "Convertendo para .mp4"
            )
            _update_job(job["id"], status=STATUS_PROCESSING, message=label)

    options: Dict[str, Any] = {
        "outtmpl": _build_outtmpl(job),
        # Melhor video + melhor audio; o ffmpeg mescla os dois em seguida.
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        # Garante .mp4 mesmo quando a origem entrega um container unico (webm/mkv).
        "postprocessors": [
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
        ],
        "restrictfilenames": True,
        "windowsfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "consoletitle": False,
        "retries": 3,
        "fragment_retries": 5,
        "socket_timeout": 30,
        # Sem paralelismo de fragmentos: mantem o perfil de trafego discreto.
        "concurrent_fragment_downloads": 1,
        "ignoreerrors": False,
        "logger": _SilentLogger(),
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
    }

    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        options["ffmpeg_location"] = ffmpeg_path
    if COOKIES_FROM_BROWSER:
        options["cookiesfrombrowser"] = (COOKIES_FROM_BROWSER,)
    if COOKIES_FILE:
        options["cookiefile"] = COOKIES_FILE

    return options


def _resolve_output_path(info: Dict[str, Any]) -> Optional[Path]:
    """Caminho final do arquivo em disco, ja depois dos pos-processadores."""
    candidates: List[str] = []
    for entry in info.get("requested_downloads") or []:
        candidates += [entry.get("filepath"), entry.get("_filename")]
    candidates += [info.get("filepath"), info.get("_filename")]

    for candidate in filter(None, candidates):
        path = Path(candidate)
        if path.exists():
            return path
        # O remuxer troca a extensao depois do download; procura o .mp4 gerado.
        remuxed = path.with_suffix(".mp4")
        if remuxed.exists():
            return remuxed

    return next((Path(c) for c in candidates if c), None)


def _download(job: Dict[str, Any]) -> None:
    with YoutubeDL(_ydl_options(job)) as ydl:
        info = ydl.extract_info(job["url"], download=True)
        if info is None:
            raise DownloadError("Nao foi possivel extrair informacoes do link")
        info = ydl.sanitize_info(info)

    path = _resolve_output_path(info)
    _update_job(
        job["id"],
        status=STATUS_DONE,
        progress=100.0,
        message="Concluido",
        title=info.get("title"),
        thumbnail=info.get("thumbnail"),
        duration=info.get("duration"),
        filename=path.name if path else None,
        filepath=str(path) if path else None,
        filesize=_human_size(path.stat().st_size) if path and path.exists() else None,
        speed=None,
        eta=None,
        error=None,
        finished_at=_now(),
    )


def _friendly_error(raw: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", raw or "").strip()
    text = text.replace("ERROR: ", "")
    lowered = text.lower()
    if "login" in lowered or "cookies" in lowered or "rate-limit" in lowered:
        return (
            "Conteudo privado ou bloqueio por rate limit. "
            "Rode com COOKIES_FROM_BROWSER=chrome para autenticar."
        )
    if "ffmpeg" in lowered and ("not installed" in lowered or "not found" in lowered):
        return f"ffmpeg nao encontrado. Instale com: {FFMPEG_INSTALL_HINT}"
    if "unsupported url" in lowered:
        return "URL nao suportada pelo yt-dlp."
    return text[:300] or "Falha desconhecida no download."


# --------------------------------------------------------------------------- #
# Worker sequencial
# --------------------------------------------------------------------------- #


def _worker_loop() -> None:
    while True:
        job_id = WORK_QUEUE.get()
        try:
            job = _get_job(job_id)
            if job is None:
                continue

            _update_job(job_id, status=STATUS_DOWNLOADING, message="Iniciando", error=None)
            try:
                _download(job)
            except Exception as exc:  # noqa: BLE001 — um job com erro nao pode derrubar a fila
                _update_job(
                    job_id,
                    status=STATUS_ERROR,
                    message="Erro",
                    error=_friendly_error(str(exc)),
                    speed=None,
                    eta=None,
                    finished_at=_now(),
                )
        finally:
            WORK_QUEUE.task_done()

        # Respiro entre itens: reduz risco de bloqueio de IP.
        if not WORK_QUEUE.empty():
            time.sleep(DELAY_BETWEEN_DOWNLOADS)


def _start_worker() -> None:
    thread = threading.Thread(target=_worker_loop, name="download-worker", daemon=True)
    thread.start()


# --------------------------------------------------------------------------- #
# Rotas
# --------------------------------------------------------------------------- #


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify(
        {
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "ffmpeg_hint": FFMPEG_INSTALL_HINT,
            "download_dir": str(DOWNLOAD_DIR),
            "delay": DELAY_BETWEEN_DOWNLOADS,
            # Booleano em vez do caminho: o valor de COOKIES_FILE aponta para um
            # arquivo de credenciais e nao precisa transitar pela rede.
            "cookies": bool(COOKIES_FROM_BROWSER or COOKIES_FILE),
            "allowed_hosts": sorted(ALLOWED_URL_HOSTS),
        }
    )


@app.post("/api/queue")
def enqueue_links():
    """Carrega o conteudo do textarea na fila visual (ainda sem baixar)."""
    payload = request.get_json(silent=True) or {}
    raw = payload.get("links", "")

    lines = raw.splitlines() if isinstance(raw, str) else list(raw)
    if len(lines) > MAX_LINKS_PER_REQUEST:
        return (
            jsonify(
                {
                    "error": f"Envie no maximo {MAX_LINKS_PER_REQUEST} links por vez."
                }
            ),
            413,
        )

    added, duplicated, invalid = [], 0, 0

    with JOBS_LOCK:
        existing = {
            job["url"]
            for job in JOBS.values()
            if job["status"] != STATUS_ERROR
        }
        for line in lines:
            url = str(line).strip().strip(",;")
            if not url:
                continue
            if not _is_allowed_url(url):
                invalid += 1
                continue
            if url in existing:
                duplicated += 1
                continue
            job = _new_job(url)
            JOBS[job["id"]] = job
            existing.add(url)
            added.append(job)

    return jsonify(
        {
            "added": len(added),
            "duplicated": duplicated,
            "invalid": invalid,
            "jobs": _snapshot(),
        }
    )


@app.post("/api/process")
def process_queue():
    """Empurra todos os itens pendentes para a thread worker."""
    with JOBS_LOCK:
        pending = [job for job in JOBS.values() if job["status"] == STATUS_PENDING]
        for job in pending:
            job["status"] = STATUS_QUEUED
            job["message"] = "Na fila"

    for job in pending:
        WORK_QUEUE.put(job["id"])

    return jsonify({"started": len(pending), "jobs": _snapshot()})


@app.post("/api/retry/<job_id>")
def retry_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "Job nao encontrado"}), 404
        if job["status"] in ACTIVE_STATUSES:
            return jsonify({"error": "Job ainda em andamento"}), 409
        job.update(
            status=STATUS_QUEUED,
            progress=0.0,
            message="Na fila",
            error=None,
            finished_at=None,
        )

    WORK_QUEUE.put(job_id)
    return jsonify({"jobs": _snapshot()})


@app.get("/api/status")
def status():
    jobs = _snapshot()
    counts = {
        "total": len(jobs),
        "pending": sum(1 for j in jobs if j["status"] == STATUS_PENDING),
        "queued": sum(1 for j in jobs if j["status"] == STATUS_QUEUED),
        "downloading": sum(1 for j in jobs if j["status"] == STATUS_DOWNLOADING),
        "processing": sum(1 for j in jobs if j["status"] == STATUS_PROCESSING),
        "done": sum(1 for j in jobs if j["status"] == STATUS_DONE),
        "error": sum(1 for j in jobs if j["status"] == STATUS_ERROR),
    }
    counts["busy"] = counts["queued"] + counts["downloading"] + counts["processing"]
    return jsonify({"jobs": jobs, "counts": counts})


@app.post("/api/clear")
def clear_jobs():
    """Remove itens finalizados. `scope=all` limpa tudo que nao esta rodando."""
    payload = request.get_json(silent=True) or {}
    scope = payload.get("scope", "done")
    removable = {STATUS_DONE} if scope == "done" else {STATUS_DONE, STATUS_ERROR, STATUS_PENDING}

    with JOBS_LOCK:
        for job_id in [jid for jid, j in JOBS.items() if j["status"] in removable]:
            del JOBS[job_id]

    return jsonify({"jobs": _snapshot()})


def _job_file(job_id: str) -> Optional[Path]:
    """
    Caminho em disco do .mp4 de um job concluido.

    Retorna None quando o job nao existe, ainda nao terminou, sumiu do disco ou
    aponta para fora de benchmarks_videos - essa ultima e a trava que impede um
    job_id forjado de transformar as rotas em leitor de arquivos arbitrarios.
    """
    job = _get_job(job_id)
    if job is None or job["status"] != STATUS_DONE or not job.get("filepath"):
        return None

    path = Path(job["filepath"]).resolve()
    root = DOWNLOAD_DIR.resolve()
    if root != path.parent and root not in path.parents:
        return None
    return path if path.is_file() else None


@app.get("/api/file/<job_id>")
def serve_file(job_id: str):
    """Entrega o .mp4 concluido para o download nativo do navegador."""
    path = _job_file(job_id)
    if path is None:
        return jsonify({"error": "Arquivo indisponivel"}), 404
    return send_file(path, as_attachment=True, download_name=path.name)


@app.get("/api/preview/<job_id>")
def preview_file(job_id: str):
    """
    Mesmo arquivo servido inline, alimentando o <video> de preview do card.

    `conditional=True` habilita Range requests: sem isso o navegador precisaria
    baixar o arquivo inteiro antes de exibir o primeiro quadro.
    """
    path = _job_file(job_id)
    if path is None:
        return jsonify({"error": "Arquivo indisponivel"}), 404
    return send_file(path, mimetype="video/mp4", conditional=True)


@app.post("/api/reveal/<job_id>")
def reveal_file(job_id: str):
    """Abre o gerenciador de arquivos do sistema na pasta do video."""
    path = _job_file(job_id)
    if path is None:
        return jsonify({"error": "Arquivo indisponivel"}), 404

    try:
        if sys.platform == "win32":
            # Caminho absoluto de proposito: resolver "explorer" pelo PATH
            # permitiria que um executavel plantado numa pasta anterior ao
            # System32 fosse chamado no lugar do original.
            explorer = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "explorer.exe"
            # O explorer devolve codigo 1 mesmo quando abre a janela, entao
            # check=True aqui daria falso negativo.
            subprocess.run([str(explorer), f"/select,{path}"], check=False)
        elif sys.platform == "darwin":
            subprocess.run(["/usr/bin/open", "-R", str(path)], check=True)
        else:
            subprocess.run(["xdg-open", str(path.parent)], check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify({"error": f"Nao foi possivel abrir a pasta: {exc}"}), 500

    return jsonify({"opened": str(path.parent)})


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #

_start_worker()


def _port_already_serving() -> bool:
    """Detecta uma instancia ja rodando na porta.

    No Windows o SO_REUSEADDR do Werkzeug permite que um segundo processo faca
    bind na mesma porta sem erro. Como cada instancia sobe a sua propria thread
    worker, isso quebraria silenciosamente o requisito de download sequencial.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((HOST, PORT)) == 0


if __name__ == "__main__":
    if _port_already_serving():
        print(
            f"\n[ERRO] Ja existe um servidor rodando em http://{HOST}:{PORT}\n"
            "        Encerre a instancia anterior (Ctrl+C no terminal dela)\n"
            "        ou rode em outra porta:  PORT=5002 python app.py\n"
        )
        sys.exit(1)

    if not shutil.which("ffmpeg"):
        print(f"\n[AVISO] ffmpeg nao encontrado no PATH. Instale com: {FFMPEG_INSTALL_HINT}\n")

    print(f"  Videos serao salvos em: {DOWNLOAD_DIR}")
    print(f"  Servidor: http://{HOST}:{PORT}\n")

    # use_reloader=False: o reloader do Flask criaria um segundo processo e,
    # com ele, uma segunda thread worker — quebrando a garantia sequencial.
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
