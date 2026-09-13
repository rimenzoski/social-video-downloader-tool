# Benchmark Video Archiver

Ferramenta web **local** para baixar vídeos de Instagram, Facebook e Reddit em `.mp4`, pensada para montar bibliotecas de referência de design (benchmarks).

- Input em lote: cole dezenas de links de uma vez em um `<textarea>`.
- Fila visual em Bento Box com status e barra de progresso por item.
- Processamento **estritamente sequencial** (um download por vez) para evitar bloqueio de IP.
- Saída sempre em `.mp4` (`yt-dlp` + `ffmpeg` mesclando áudio e vídeo).
- Arquivos salvos em `benchmarks_videos/` com timestamp + ID único no nome.
- Ao final do lote, todos os vídeos são entregues de uma vez para o mesmo destino.

---

## Estrutura do projeto

```
social-video-downloader-tool/
├── app.py                     # Backend Flask + fila sequencial (thread worker única)
├── requirements.txt           # Dependências Python
├── README.md
├── .gitignore
├── benchmarks_videos/         # Destino dos .mp4 (criada automaticamente)
├── templates/
│   └── index.html             # Interface (Tailwind via CDN)
└── static/
    └── js/
        └── main.js            # Parsing do textarea, polling e render dos cards
```

---

> **Importante:** esta é uma aplicação cliente-servidor. Abrir o `templates/index.html`
> direto no navegador **não funciona** — o `main.js` não carrega e não há backend para
> receber os links. Sempre inicie o servidor e acesse `http://127.0.0.1:5001`.

---

## Passo a passo (macOS · Apple Silicon M1 Pro)

### 1. Instalar o Homebrew (pule se já tiver)

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

No Apple Silicon o Homebrew instala em `/opt/homebrew`. Se o comando `brew` não for reconhecido depois da instalação, adicione ao PATH:

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

### 2. Instalar o ffmpeg (obrigatório)

Sem ele o `yt-dlp` não consegue mesclar as faixas de áudio e vídeo separadas nem garantir a saída em `.mp4`.

```bash
brew install ffmpeg
```

Confirme a instalação:

```bash
ffmpeg -version
```

### 3. Entrar na pasta do projeto

```bash
cd ~/Desktop/social-video-downloader-tool
```

### 4. Criar e ativar o ambiente virtual

```bash
python3 -m venv venv
source venv/bin/activate
```

### 5. Instalar as dependências Python

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 6. Rodar o servidor

```bash
python app.py
```

Saída esperada:

```
  Videos serao salvos em: /Users/voce/Desktop/social-video-downloader-tool/benchmarks_videos
  Servidor: http://127.0.0.1:5001
```

### 7. Abrir a interface

Acesse **http://127.0.0.1:5001** no navegador.

> A porta é a **5001** porque no macOS a 5000 é ocupada pelo AirPlay Receiver. Se preferir a 5000, desative em *Ajustes do Sistema → Geral → AirDrop e Handoff → Receptor AirPlay*.

### 8. Usar

1. Cole os links no textarea, um por linha.
2. Clique em **Carregar Links** — cada URL vira um card com status `Aguardando`.
3. Na barra da fila, clique em **Processar Fila** — os downloads rodam um por vez, sem pedir nada.
4. Ao terminar o lote, um único modal aparece para entregar todos os vídeos de uma vez.

Todas as ações que operam sobre a fila (**Processar Fila**, **Salvar vídeos**, **Limpar concluídos** e **Limpar tudo**) ficam na barra acima do grid; o card de input guarda apenas **Carregar Links**.

Cada card concluído mostra um preview do vídeo que **dá play ao passar o mouse**, o caminho completo do arquivo em disco e um botão **Abrir pasta**, que abre o Explorer (ou Finder) já com o arquivo selecionado.

Para encerrar: `Ctrl + C` no terminal e depois `deactivate` para sair do venv.

---

## Passo a passo (Windows · PowerShell)

O código é multiplataforma; só mudam o gerenciador de pacotes e a ativação do venv.

```powershell
# 1. Python e ffmpeg (winget faz o papel do Homebrew)
winget install Python.Python.3.13
winget install Gyan.FFmpeg

# 2. Feche e reabra o PowerShell para o PATH ser atualizado, depois:
cd $HOME\Desktop\social-video-downloader-tool
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Se o `Activate.ps1` for bloqueado pela política de execução, rode uma vez:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Alternativa sem ativar o venv (útil em scripts): chame o interpretador direto com
`.\venv\Scripts\python.exe app.py`.

Para conteúdo privado, o equivalente ao prefixo de variável do bash é:

```powershell
$env:COOKIES_FROM_BROWSER="chrome"; python app.py
```

---

## Escolhendo onde salvar os vídeos

1. Clique em **Processar Fila** — os downloads rodam um por vez, sem pedir pasta.
2. Quando **todo o lote terminar**, aparece o modal **“Downloads concluídos”**.
3. Clique em **Salvar vídeos**. Os arquivos são entregues pelo download nativo do navegador, um a um, com 800 ms de intervalo.
4. Um toast verde confirma quantos vídeos foram salvos.

Como quem decide a pasta é o navegador, e não a página, o lote inteiro cai automaticamente no mesmo lugar. Se fechar com “Agora não”, o botão **Salvar vídeos** continua ao lado de “Processar Fila”.

Internamente, o yt-dlp grava primeiro em `benchmarks_videos/` (pasta temporária); a entrega para o destino final acontece só no passo 3.

Para trocar a pasta de destino, ajuste em *Configurações → Downloads* do seu navegador. Deixe **“Perguntar onde salvar cada arquivo”** desligado para que o lote inteiro seja salvo sem diálogos.

---

## Conteúdo privado ou com login

Instagram e Facebook bloqueiam boa parte do conteúdo para visitantes anônimos. Nesses casos, rode o servidor reaproveitando os cookies do seu navegador:

```bash
COOKIES_FROM_BROWSER=chrome python app.py
```

Valores aceitos: `chrome`, `safari`, `firefox`, `brave`, `edge`. Alternativamente, use um arquivo no formato Netscape:

```bash
COOKIES_FILE=~/cookies.txt python app.py
```

---

## Variáveis de ambiente

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `PORT` | `5001` | Porta do servidor local. |
| `DOWNLOAD_DELAY` | `3` | Segundos de pausa entre downloads consecutivos. |
| `COOKIES_FROM_BROWSER` | — | Navegador de onde extrair cookies. |
| `COOKIES_FILE` | — | Caminho para um `cookies.txt`. |
| `EXTRA_ALLOWED_HOSTS` | — | Domínios adicionais aceitos, separados por vírgula. |

Exemplo com intervalo maior entre itens (lotes grandes de Instagram):

```bash
DOWNLOAD_DELAY=8 COOKIES_FROM_BROWSER=chrome python app.py
```

---

## Segurança

O app escuta apenas em `127.0.0.1`, mas "só localhost" não é, por si só, uma
fronteira de segurança: qualquer página aberta no navegador consegue enviar
requisições para portas locais. As defesas abaixo tratam disso.

| Proteção | O que ela impede |
| --- | --- |
| Validação do header `Host` | **DNS rebinding.** Um site externo pode apontar o próprio domínio para `127.0.0.1`; o navegador então trata a API como mesma origem e o site consegue *ler* as respostas (fila, caminhos em disco, os próprios vídeos). Só `127.0.0.1`, `localhost` e `[::1]` na porta configurada são aceitos. |
| Exigência de `Sec-Fetch-Site: same-origin` (ou `Origin` local) em toda requisição que muda estado | **CSRF.** Um `<form>` em outro site dispara `POST` sem passar por preflight CORS, e rotas como `/api/process`, `/api/clear` e `/api/reveal` não dependem do corpo — seriam acionáveis por qualquer aba aberta. |
| Allowlist de domínios nos links | **SSRF.** Antes, qualquer `http(s)://` era aceito, incluindo `169.254.169.254` (metadados de nuvem) e IPs da rede local. A checagem é por sufixo de domínio, então `instagram.com.evil.net` é recusado. |
| `_job_file()` resolve o caminho e confirma que ele está sob `benchmarks_videos/` | **Path traversal.** Um `job_id` forjado não transforma `/api/file`, `/api/preview` e `/api/reveal` em leitor de arquivos arbitrários. |
| `MAX_CONTENT_LENGTH` de 256 KB e teto de 200 links por requisição | Corpos gigantes consumindo memória do processo. |
| `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` e CSP | Sniffing de MIME, clickjacking e carregamento de recursos de terceiros. |
| Caminho absoluto de `explorer.exe` / `open` em `/api/reveal` | **PATH hijacking:** um executável plantado numa pasta anterior ao `System32` seria chamado no lugar do original. |
| `/api/health` devolve apenas um booleano para cookies | Vazamento do caminho do arquivo de credenciais. |

Notas de operação:

- `debug=False` é obrigatório. O debugger do Werkzeug expõe um console Python que
  permite execução remota de código.
- Nunca publique este app em `0.0.0.0` nem atrás de um túnel. As rotas não têm
  autenticação, e `/api/reveal` executa um programa do sistema operacional.
- `cookies.txt` e `.env` estão no `.gitignore`. Se você usar `COOKIES_FILE`,
  mantenha o arquivo fora do repositório: ele equivale à sua sessão logada.

---

## Rotas da API

| Método | Rota | Descrição |
| --- | --- | --- |
| `GET` | `/` | Interface web. |
| `GET` | `/api/health` | Verifica ffmpeg, pasta de destino e configuração. |
| `POST` | `/api/queue` | Recebe `{ "links": "url1\nurl2" }` e cria os jobs pendentes. |
| `POST` | `/api/process` | Envia todos os pendentes para a thread worker. |
| `GET` | `/api/status` | Estado de todos os jobs + contadores. |
| `POST` | `/api/retry/<job_id>` | Reenfileira um job que falhou. |
| `GET` | `/api/file/<job_id>` | Entrega o `.mp4` concluído para o download do navegador. |
| `GET` | `/api/preview/<job_id>` | Mesmo arquivo servido inline, com Range, para o preview do card. |
| `POST` | `/api/reveal/<job_id>` | Abre o gerenciador de arquivos na pasta do vídeo. |
| `POST` | `/api/clear` | `{ "scope": "done" \| "all" }` remove jobs finalizados. |

---

## Manutenção e solução de problemas

**Download falha em links que antes funcionavam.** Redes sociais mudam a estrutura das páginas com frequência. Atualize o motor de download:

```bash
pip install -U yt-dlp
```

**"ffmpeg nao encontrado".** O `brew install ffmpeg` não foi executado ou o PATH do Homebrew não está carregado no shell atual. Rode `which ffmpeg` para conferir.

**Erro pedindo login/cookies.** O post é privado ou o IP foi limitado. Use `COOKIES_FROM_BROWSER` e aumente o `DOWNLOAD_DELAY`.

**Vários erros seguidos no mesmo lote.** Sinal clássico de rate limit. Pare, espere alguns minutos e reprocesse com `DOWNLOAD_DELAY=10`.

**Cliquei nos botões e nada acontece.** Você abriu o `index.html` como arquivo em vez de acessar `http://127.0.0.1:5001`. Sem o Flask renderizando a página, o `main.js` nunca é carregado e nenhum botão tem comportamento.

**"Ja existe um servidor rodando".** Outra instância está ocupando a porta. Encerre-a com `Ctrl + C` ou use outra porta: `PORT=5002 python app.py`.

---

## Notas de arquitetura

O estado da fila vive em memória (`OrderedDict` protegido por lock) e é consumido por **uma única thread worker** daemon. Isso é o que garante o requisito de execução em série: mesmo que a interface envie 50 links de uma vez, apenas um `yt-dlp` roda por vez, com uma pausa configurável entre eles.

O Flask roda com `use_reloader=False` de propósito — o reloader criaria um segundo processo e, junto com ele, uma segunda thread worker, quebrando a garantia sequencial.

Como o estado é em memória, reiniciar o servidor limpa a fila. Os vídeos já baixados permanecem em `benchmarks_videos/`.

---

## Aviso de uso

Ferramenta de uso pessoal para documentação e estudo de referências de design. Respeite os termos de serviço de cada plataforma e os direitos autorais dos criadores ao arquivar e reutilizar o material.
