# 🎬 ECO Captions — Documentação do Sistema

Bem-vindo à documentação oficial do **ECO Captions**. Este arquivo descreve a arquitetura, lógica de negócios, fluxo de dados e estrutura da aplicação de ponta a ponta.

---

## 🎯 Visão Geral

O **ECO Captions** é uma aplicação web que transcreve vídeos automaticamente usando o Google Gemini e gera legendas estilo Reels gravadas diretamente no vídeo, com total controle de estilo pelo operador.

**Fluxo principal:** Upload → Transcrição (Gemini) → Corte de silêncios (FFmpeg) → Edição da transcrição → Personalização → Vídeo com legenda gravada

---

## 🛠️ Stack Tecnológico

### Backend
- **Runtime:** Python 3.11+
- **Framework:** FastAPI + Uvicorn
- **Transcrição:** Google Gemini 2.5-flash (API multimodal — envia o áudio, recebe JSON com segmentos e timestamps)
- **Processamento de vídeo:** FFmpeg (extração de áudio, corte de silêncios, burn de legendas)
- **Formato de legenda:** ASS (Advanced SubStation Alpha) — posicionamento pixel-preciso via PlayResX/Y
- **Jobs:** In-memory dict (stateless por sessão)

### Frontend
- HTML/CSS/JS vanilla — arquivo único em `static/index.html`
- Drag-and-drop de vídeo, editor de transcrição inline, painel de personalização

### Infraestrutura
- **Deploy:** Railway (auto-deploy via push no GitHub)
- **FFmpeg no Railway:** instalado via `nixpacks.toml`
- **Porta:** variável de ambiente `$PORT` (padrão local: 8000)

---

## ⚙️ Arquitetura e Fluxo de Dados

### Pipeline de Processamento (`POST /process/{job_id}`)
Roda em background thread após o upload:

1. **Extração de áudio** — FFmpeg extrai WAV 16kHz mono do vídeo
2. **Transcrição** — Gemini 2.5-flash recebe o WAV via Files API e retorna JSON com segmentos `{start, end, text}`
3. **Corte de silêncios** — detecta intervalos de fala, mescla com gap mínimo de 0.5s, corta o vídeo via `filter_complex_script`
4. **Remapeamento de timestamps** — ajusta os timestamps dos segmentos para o novo vídeo cortado
5. Status vira `transcribed` → frontend exibe editor de transcrição

### Pipeline de Renderização (`POST /render/{job_id}`)
Roda em background thread após o operador ajustar a transcrição e escolher o estilo:

1. Divide cada segmento em chunks de N palavras com timing proporcional (`segments_to_word_entries`)
2. Gera arquivo `subs.ass` com as opções de estilo escolhidas
3. FFmpeg grava as legendas no vídeo via filtro `ass=subs.ass`
4. Status vira `done` → frontend libera download

---

## 🎨 Personalização de Legendas

O operador pode ajustar antes de renderizar:

| Opção | Valores | Padrão |
|-------|---------|--------|
| Cor do texto | Color picker (#hex) | `#ffffff` |
| Cor do contorno | Color picker (#hex) | `#000000` |
| Fonte | Arial, Impact, Segoe UI, Calibri, Verdana, Tahoma, Arial Black | `Arial` |
| Tamanho | P / M / G / XG | `G` |
| Posição | Topo / Centro / Baixo | `Baixo` |
| Palavras por bloco | 1 / 2 / 3 / 4 | `3` |

### Tamanhos de fonte (pixels ASS, escala 1:1 com o vídeo)
| Tamanho | Vertical | Horizontal |
|---------|----------|------------|
| P (pequeno) | 40 | 28 |
| M (médio) | 52 | 36 |
| G (grande) | 62 | 42 |
| XG (extra) | 78 | 55 |

### Posicionamento ASS
| Posição | Alignment | MarginV |
|---------|-----------|---------|
| Topo | 8 | 40 |
| Centro | 5 | 0 |
| Baixo | 2 | 80 |

---

## 📡 API Endpoints

| Método | Rota | Descrição |
|--------|------|-----------|
| `POST` | `/upload` | Recebe o arquivo de vídeo, cria job, retorna `job_id` |
| `POST` | `/process/{job_id}` | Inicia pipeline de transcrição em background |
| `GET` | `/status/{job_id}` | Retorna status, progresso e mensagem do job |
| `GET` | `/transcript/{job_id}` | Retorna segmentos transcritos |
| `PUT` | `/transcript/{job_id}` | Atualiza segmentos com edições do operador |
| `POST` | `/render/{job_id}` | Inicia renderização com opções de estilo (JSON body) |
| `GET` | `/download/{job_id}` | Download do vídeo final (`legendario_{id}.mp4`) |

### Corpo do `POST /render`
```json
{
  "color": "#ffffff",
  "outline": "#000000",
  "font": "Arial",
  "size": "grande",
  "position": "baixo",
  "chunk": 3
}
```

---

## 📂 Estrutura de Diretórios

```
/
├── app.py                # Backend completo (FastAPI + pipeline)
├── requirements.txt      # Dependências Python
├── Procfile              # Comando de start para Railway
├── nixpacks.toml         # Instalação do FFmpeg no Railway
├── .env                  # Variáveis de ambiente (não commitado)
├── .env.example          # Template de variáveis
├── .gitignore
├── uploads/              # Diretório de jobs (gerado em runtime, não commitado)
│   └── {job_id}/
│       ├── input.mp4     # Vídeo original
│       ├── audio.wav     # Áudio extraído (16kHz mono)
│       ├── cut.mp4       # Vídeo sem silêncios
│       ├── subs.ass      # Arquivo de legendas gerado
│       └── final.mp4     # Vídeo final com legenda gravada
└── static/
    └── index.html        # Frontend completo (HTML/CSS/JS)
```

---

## 🚀 Como Executar Localmente

### Pré-requisitos
- Python 3.11+
- FFmpeg instalado e no PATH
- Chave de API do Google Gemini

### Instalação
```bash
pip install -r requirements.txt
```

### Configuração
Crie o `.env` baseado no `.env.example`:
```env
GEMINI_KEY=sua_chave_aqui
```

### Executar
```bash
python app.py
```
Acesse: `http://localhost:8000`

### Matar processo na porta 8000 (Windows)
```powershell
$proc = Get-NetTCPConnection -LocalPort 8000 | Select-Object -ExpandProperty OwningProcess -First 1
Stop-Process -Id $proc -Force
```

---

## ☁️ Deploy (Railway)

O projeto está configurado para deploy automático no Railway:

- **`Procfile`** define o comando de start com `$PORT` dinâmico
- **`nixpacks.toml`** instala o FFmpeg no container
- Qualquer push na branch `master` dispara um novo deploy automaticamente

**Variável de ambiente obrigatória no Railway:**
```
GEMINI_KEY=sua_chave_aqui
```

**URL de produção:** https://eco-captions-production.up.railway.app

---

## ⚠️ Limitações Conhecidas

- **Jobs em memória:** reiniciar o servidor apaga todos os jobs em andamento — o operador precisa fazer novo upload
- **Filesystem efêmero:** no Railway, os arquivos em `uploads/` são perdidos em cada deploy (aceitável para uso operacional)
- **Timestamps do Gemini:** a precisão dos timestamps depende da resposta da API — o editor de transcrição permite corrigir antes de renderizar
- **Fontes no Railway:** apenas fontes disponíveis no container Linux (Arial, Verdana etc. podem ter substitutos)

---

*Documentação mantida para o sistema ECO Captions — Eco Digital*
