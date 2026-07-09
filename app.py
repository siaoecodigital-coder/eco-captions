import os
import json
import uuid
import subprocess
import threading
from pathlib import Path

import re
import sys
import traceback

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import uvicorn
from dotenv import load_dotenv

from google import genai

load_dotenv()

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR.mkdir(exist_ok=True)

jobs = {}

SIZE_MAP = {
    "pequeno":  (40, 28),
    "medio":    (52, 36),
    "grande":   (62, 42),
    "extra":    (78, 55),
}

# (Alignment, MarginV)  Alignment: 8=top-center, 5=mid-center, 2=bottom-center
POSITION_MAP = {
    "topo":   (8, 40),
    "centro": (5, 0),
    "baixo":  (2, 120),
}

TARJA_POSITION_MAP = {
    "topo":   (8, 20),
    "centro": (5, 0),
    "baixo":  (2, 80),
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def hex_to_ass(hex_color: str) -> str:
    """Convert #RRGGBB to ASS &H00BBGGRR."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return "&H00FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def get_video_info(video_path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-show_entries", "format=duration",
        "-of", "json", video_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(r.stdout)
    s = data["streams"][0]
    return {"width": s["width"], "height": s["height"], "duration": float(data["format"]["duration"])}


def ass_time(t: float) -> str:
    h, m, s = int(t // 3600), int((t % 3600) // 60), int(t % 60)
    cs = int((t % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


# ─── Pipeline ─────────────────────────────────────────────────────────────────

def extract_audio(video_path: str, out_dir: Path) -> str:
    audio_path = str(out_dir / "audio.wav")
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", audio_path
    ], capture_output=True, check=True)
    return audio_path


def transcribe_audio(audio_path: str) -> list:
    client = genai.Client(api_key=os.getenv("GEMINI_KEY"))

    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    try:
        prompt = (
            "Transcreva este áudio em português do Brasil. "
            "Retorne SOMENTE JSON válido, sem markdown, sem explicações:\n"
            '{"segments":[{"start":0.0,"end":3.2,"text":"..."},...]}\n'
            "Regras: start/end em segundos (float), 5 a 15 palavras por segmento, cubra todo o áudio sem lacunas."
        )
        audio_part = genai.types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav")
        text_part = genai.types.Part.from_text(text=prompt)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[audio_part, text_part],
        )

        text = response.text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        data = json.loads(text)
        return [
            {"index": i, "start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip()}
            for i, s in enumerate(data.get("segments", []))
        ]
    except Exception as e:
        print(f"ERRO TRANSCRIÇÃO: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        raise


def transcribe_image(image_path: str) -> list:
    """Transcreve uma imagem via Gemini Vision — descreve o conteúdo visível."""
    client = genai.Client(api_key=os.getenv("GEMINI_KEY"))

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    ext = Path(image_path).suffix.lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")

    prompt = (
        "Descreva detalhadamente esta imagem em português do Brasil. "
        "Inclua: o que está sendo mostrado (produto, pessoa, local), textos visíveis, "
        "cores predominantes, atmosfera, e qualquer oferta ou promoção escrita. "
        "Se for um carrossel ou post de loja, descreva os produtos com seus preços se houver. "
        "Retorne SOMENTE JSON válido, sem markdown, sem explicações:\n"
        '{"segments":[{"start":0.0,"end":5.0,"text":"Descrição detalhada do que está na imagem..."}]}\n'
        "Regras: cubra todos os detalhes visuais relevantes, seja específico sobre produtos, preços e marcas."
    )
    image_part = genai.types.Part.from_bytes(data=image_bytes, mime_type=mime)
    text_part = genai.types.Part.from_text(text=prompt)

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[image_part, text_part],
        )
        text = response.text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        data = json.loads(text)
        return [
            {"index": i, "start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip()}
            for i, s in enumerate(data.get("segments", []))
        ]
    except Exception as e:
        print(f"ERRO IMAGE TRANSCRIÇÃO: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        raise


def find_speech_intervals(segments: list, min_gap=0.5, padding=0.05) -> list:
    if not segments:
        return []
    intervals = [(max(0, s["start"] - padding), s["end"] + padding) for s in segments]
    merged = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start - merged[-1][1] < min_gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [tuple(i) for i in merged]


def cut_silences(video_path: str, intervals: list, out_dir: Path) -> str:
    if not intervals:
        return video_path
    output_path = str(out_dir / "cut.mp4")
    n = len(intervals)
    parts = []
    for i, (s, e) in enumerate(intervals):
        parts.append(f"[0:v]trim={s:.3f}:{e:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS[a{i}]")
    concat = "".join(f"[v{i}][a{i}]" for i in range(n))
    parts.append(f"{concat}concat=n={n}:v=1:a=1[vout][aout]")
    script = out_dir / "cut_filter.txt"
    script.write_text(";\n".join(parts))
    result = subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-filter_complex_script", str(script),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k", output_path
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg corte falhou:\n{result.stderr[-2000:]}")
    return output_path


def remap_timestamps(segments: list, intervals: list) -> list:
    cum = 0
    idata = []
    for s, e in intervals:
        idata.append((s, e, cum))
        cum += e - s
    result = []
    for seg in segments:
        new_s = new_e = None
        for s, e, offset in idata:
            if s <= seg["start"] <= e and new_s is None:
                new_s = offset + (seg["start"] - s)
            if s <= seg["end"] <= e and new_e is None:
                new_e = offset + (seg["end"] - s)
        if new_s is None:
            continue
        if new_e is None:
            new_e = new_s + (seg["end"] - seg["start"])
        result.append({**seg, "start": round(max(0, new_s), 3), "end": round(max(0, new_e), 3)})
    return result


def segments_to_word_entries(segments: list, chunk_size: int = 3) -> list:
    entries = []
    for seg in segments:
        words = seg["text"].split()
        if not words:
            continue
        duration = seg["end"] - seg["start"]
        word_dur = duration / len(words)
        for i in range(0, len(words), chunk_size):
            chunk = words[i:i + chunk_size]
            start = seg["start"] + i * word_dur
            end = min(seg["start"] + (i + len(chunk)) * word_dur, seg["end"])
            entries.append({"start": start, "end": end, "text": " ".join(chunk)})
    return entries


def build_ass(entries: list, width: int, height: int, options: dict, tarja_options: dict = None, video_duration: float = None) -> str:
    is_vertical = height > width
    size_key = options.get("size", "grande")
    size_v, size_h = SIZE_MAP.get(size_key, SIZE_MAP["grande"])
    font_size = size_v if is_vertical else size_h

    pos_key = options.get("position", "baixo")
    alignment, margin_v = POSITION_MAP.get(pos_key, POSITION_MAP["baixo"])

    font = options.get("font", "Arial")
    primary = hex_to_ass(options.get("color", "#ffffff"))
    outline = hex_to_ass(options.get("outline", "#000000"))

    styles = f"Style: Default,{font},{font_size},{primary},&H000000FF,{outline},&H00000000,-1,0,0,0,100,100,0,0,1,4,0,{alignment},20,20,{margin_v},1\n"

    tarja_event = ""

    if tarja_options and tarja_options.get("text", "").strip():
        t = tarja_options
        t_size_v, t_size_h = SIZE_MAP.get(t.get("size", "medio"), SIZE_MAP["medio"])
        t_font_size = t_size_v if is_vertical else t_size_h
        t_align, t_margin = TARJA_POSITION_MAP.get(t.get("position", "topo"), TARJA_POSITION_MAP["topo"])
        t_primary = hex_to_ass(t.get("color", "#ffffff"))
        t_bg = t.get("bg", "#000000")

        if t_bg.lower() == "transparent":
            t_border_style = 1
            t_back_color = "&H00000000"
            t_outline_color = hex_to_ass("#000000")
            t_outline_size = 3
        else:
            t_border_style = 3
            t_back_color = hex_to_ass(t_bg)
            t_outline_color = hex_to_ass(t_bg)
            t_outline_size = 4

        t_font = t.get("font", "Arial")

        styles += f"Style: Tarja,{t_font},{t_font_size},{t_primary},&H000000FF,{t_outline_color},{t_back_color},-1,0,0,0,100,100,0,0,{t_border_style},{t_outline_size},0,{t_align},20,20,{t_margin},1\n"

        dur = video_duration or 9999
        t_an_tag = "{\\an" + str(t_align) + "}"
        tarja_event = f"Dialogue: 1,{ass_time(0)},{ass_time(dur)},Tarja,,0,0,0,,{t_an_tag}{t['text'].strip()}\n"

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
""" + styles + """
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    an_tag = "{\\an" + str(alignment) + "}"
    events = "\n".join(
        f"Dialogue: 0,{ass_time(e['start'])},{ass_time(e['end'])},Default,,0,0,0,,{an_tag}{e['text'].strip()}"
        for e in entries
        if e.get("text", "").strip()
    )
    return header + tarja_event + events + "\n"


def render_video(cut_video: str, segments: list, out_dir: Path, options: dict) -> str:
    output_path = str(out_dir / "final.mp4")
    info = get_video_info(cut_video)
    w, h = info["width"], info["height"]
    duration = info["duration"]

    chunk_size = int(options.get("chunk", 3))
    entries = segments_to_word_entries(segments, chunk_size=chunk_size)

    tarja_opts = {
        "text": options.get("tarja_text"),
        "color": options.get("tarja_color", "#ffffff"),
        "bg": options.get("tarja_bg", "#000000"),
        "font": options.get("tarja_font", "Arial"),
        "size": options.get("tarja_size", "medio"),
        "position": options.get("tarja_position", "topo"),
    }

    ass_content = build_ass(
        entries, w, h, options,
        tarja_options=tarja_opts if tarja_opts["text"] and str(tarja_opts["text"]).strip() else None,
        video_duration=duration
    )
    ass_path = out_dir / "subs.ass"
    ass_path.write_text(ass_content, encoding="utf-8")

    result = subprocess.run([
        "ffmpeg", "-y", "-i", cut_video,
        "-vf", "ass=subs.ass",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "copy", output_path
    ], capture_output=True, text=True, cwd=str(out_dir))

    if result.returncode != 0:
        raise RuntimeError(f"Render falhou:\n{result.stderr[-2000:]}")
    return output_path


# ─── Background workers ────────────────────────────────────────────────────────

def run_pipeline(job_id: str):
    job = jobs[job_id]
    out_dir = Path(job["out_dir"])
    media_type = job.get("media_type", "video")

    def upd(step, msg, pct):
        jobs[job_id].update({"step": step, "message": msg, "progress": pct})

    try:
        if media_type == "image":
            # Imagem: Gemini Vision descreve, sem corte de silêncio (não é vídeo)
            upd(1, "Analisando imagem com IA...", 30)
            segments = transcribe_image(job["video_path"])

            jobs[job_id].update({
                "status": "transcribed",
                "step": 4,
                "message": "Análise da imagem concluída! Confira e gere a legenda.",
                "progress": 100,
                "cut_path": job["video_path"],  # imagem original
                "segments": segments,
            })
            return

        # Video/Audio: pipeline normal
        upd(1, "Extraindo áudio...", 15)
        audio = extract_audio(job["video_path"], out_dir)

        upd(2, "Transcrevendo com Gemini...", 35)
        segments = transcribe_audio(audio)

        if media_type == "audio":
            # Áudio puro: sem corte de silêncio
            jobs[job_id].update({
                "status": "transcribed",
                "step": 4,
                "message": "Áudio transcrito! Confira e gere a legenda.",
                "progress": 100,
                "cut_path": job["video_path"],
                "segments": segments,
            })
            return

        # Vídeo: corte de silêncios
        upd(3, "Cortando silêncios...", 65)
        intervals = find_speech_intervals(segments)
        cut_path = cut_silences(job["video_path"], intervals, out_dir)
        segments = remap_timestamps(segments, intervals)

        jobs[job_id].update({
            "status": "transcribed",
            "step": 4,
            "message": "Transcrição pronta! Corrija se necessário e gere o vídeo.",
            "progress": 100,
            "cut_path": cut_path,
            "segments": segments,
        })

    except Exception as e:
        jobs[job_id].update({"status": "error", "message": str(e), "progress": 0})


def run_render(job_id: str, options: dict):
    job = jobs[job_id]
    out_dir = Path(job["out_dir"])

    if job.get("media_type") in ("image", "audio"):
        jobs[job_id].update({"status": "error", "message": "Renderização de vídeo indisponível para imagens/áudio. Use a aba Transcrição IA para gerar a legenda.", "progress": 0})
        return

    try:
        jobs[job_id].update({"status": "rendering", "message": "Renderizando...", "progress": 50})
        final = render_video(job["cut_path"], job["segments"], out_dir, options)
        jobs[job_id].update({"status": "done", "message": "Vídeo pronto!", "progress": 100, "final_path": final})
    except Exception as e:
        jobs[job_id].update({"status": "error", "message": str(e), "progress": 0})


# ─── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI(title="ECO Captions")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())[:8]
    out_dir = UPLOAD_DIR / job_id
    out_dir.mkdir(parents=True)
    video_path = str(out_dir / f"input{Path(file.filename).suffix}")
    with open(video_path, "wb") as f:
        f.write(await file.read())
    jobs[job_id] = {
        "status": "uploaded", "step": 0, "message": "Vídeo recebido", "progress": 0,
        "video_path": video_path, "out_dir": str(out_dir)
    }
    return {"job_id": job_id}


@app.post("/process/{job_id}")
def process(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job não encontrado")
    jobs[job_id]["status"] = "processing"
    threading.Thread(target=run_pipeline, args=(job_id,), daemon=True).start()
    return {"ok": True}


@app.get("/status/{job_id}")
def status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job não encontrado")
    job = jobs[job_id]
    return {
        "status": job["status"],
        "step": job.get("step", 0),
        "message": job.get("message", ""),
        "progress": job.get("progress", 0),
    }


@app.get("/transcript/{job_id}")
def get_transcript(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404)
    return {"segments": jobs[job_id].get("segments", [])}


class TranscriptUpdate(BaseModel):
    segments: list


@app.put("/transcript/{job_id}")
def update_transcript(job_id: str, body: TranscriptUpdate):
    if job_id not in jobs:
        raise HTTPException(404)
    jobs[job_id]["segments"] = body.segments
    return {"ok": True}


class RenderRequest(BaseModel):
    color: Optional[str] = "#ffffff"
    outline: Optional[str] = "#000000"
    font: Optional[str] = "Arial"
    size: Optional[str] = "grande"
    position: Optional[str] = "baixo"
    chunk: Optional[int] = 3
    tarja_text: Optional[str] = None
    tarja_color: Optional[str] = "#ffffff"
    tarja_bg: Optional[str] = "#000000"
    tarja_font: Optional[str] = "Arial"
    tarja_size: Optional[str] = "medio"
    tarja_position: Optional[str] = "topo"


@app.post("/render/{job_id}")
async def render(job_id: str, request: Request):
    if job_id not in jobs:
        raise HTTPException(404, "Job não encontrado")
    if jobs[job_id]["status"] not in ("transcribed", "done"):
        raise HTTPException(400, "Processe o vídeo primeiro")
    try:
        data = await request.json()
        req = RenderRequest(**data)
    except Exception:
        req = RenderRequest()
    jobs[job_id]["status"] = "transcribed"
    options = req.model_dump()
    threading.Thread(target=run_render, args=(job_id, options), daemon=True).start()
    return {"ok": True}


@app.get("/download/{job_id}")
def download(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404)
    job = jobs[job_id]
    if job["status"] != "done":
        raise HTTPException(400, "Vídeo ainda não pronto")
    path = job.get("final_path")
    if not path or not Path(path).exists():
        raise HTTPException(404, "Arquivo não encontrado")
    return FileResponse(path, media_type="video/mp4", filename=f"eco-captions_{job_id}.mp4",
                        headers={"Cache-Control": "no-store"})


# ─── Instagram download ────────────────────────────────────────────────────────

INSTAGRAM_COOKIES = os.getenv("INSTAGRAM_COOKIES", "").strip()

SUPPORTED_INSTAGRAM_PATTERNS = [
    r"/reel/",           # Reels
    r"/stories?/",       # Stories (aceita /story/ e /stories/)
    r"/p/",              # Posts (carrossel, imagem única, vídeo)
]

INSTAGRAM_USER = os.getenv("INSTAGRAM_USER", "").strip()
INSTAGRAM_PASS = os.getenv("INSTAGRAM_PASS", "").strip()


def is_instagram_supported(url: str) -> bool:
    """Verifica se a URL é um tipo suportado do Instagram."""
    return any(re.search(p, url, re.I) for p in SUPPORTED_INSTAGRAM_PATTERNS)


def clean_instagram_url(url: str) -> str:
    return re.sub(r"[?&]igsh=.*", "", url).rstrip("/")


def extract_shortcode(url: str) -> str:
    """Extrai o shortcode da URL do Instagram. Ex: /reel/DaB6DpFpmds/ → DaB6DpFpmds"""
    m = re.search(r"/(?:reel|stories?|p)/([^/?]+)", url, re.I)
    if not m:
        raise HTTPException(400, "Não foi possível extrair o identificador do conteúdo da URL.")
    return m.group(1)


def download_with_instaloader(shortcode: str, out_dir: Path, is_story: bool = False) -> dict:
    """Baixa mídia do Instagram usando instaloader autenticado. Retorna {path, media_type}."""
    from instaloader import Instaloader, Post, Story, Profile

    L = Instaloader(
        download_pictures=True,
        download_videos=True,
        download_video_thumbnails=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
    )

    # Login
    try:
        L.login(INSTAGRAM_USER, INSTAGRAM_PASS)
    except Exception as e:
        raise HTTPException(500, f"Falha na autenticação do Instagram: {str(e)}")

    try:
        if is_story:
            # Stories: shortcode é o username, baixamos os stories ativos
            profile = Profile.from_username(L.context, shortcode)
            stories_ok = 0
            media_path = None
            for story in L.get_stories(userids=[profile.userid]):
                for item in story.get_items():
                    if item.is_video:
                        L.download_storyitem(item, str(out_dir))
                        stories_ok += 1
                        if not media_path:
                            # Encontra o arquivo baixado
                            videos = list(out_dir.glob("*.mp4")) + list(out_dir.glob("*.mkv")) + list(out_dir.glob("*.webm"))
                            if videos:
                                media_path = str(videos[0])
                    else:
                        L.download_storyitem(item, str(out_dir))
                        stories_ok += 1
                        if not media_path:
                            images = list(out_dir.glob("*.jpg")) + list(out_dir.glob("*.png"))
                            if images:
                                media_path = str(images[0])

            if not media_path:
                raise HTTPException(400, f"Nenhum story ativo encontrado para @{shortcode}.")

            # Detecta tipo
            ext = Path(media_path).suffix.lower()
            media_type = "video" if ext in (".mp4", ".mkv", ".webm") else "image"
            return {"path": media_path, "media_type": media_type}

        else:
            # Posts e Reels
            post = Post.from_shortcode(L.context, shortcode)
            L.download_post(post, target=str(out_dir))

            # Encontra arquivo baixado
            videos = list(out_dir.glob("*.mp4")) + list(out_dir.glob("*.mkv")) + list(out_dir.glob("*.webm"))
            images = list(out_dir.glob("*.jpg")) + list(out_dir.glob("*.jpeg")) + list(out_dir.glob("*.png"))

            if videos:
                return {"path": str(videos[0]), "media_type": "video"}
            elif images:
                return {"path": str(images[0]), "media_type": "image"}
            else:
                raise HTTPException(400, "Download concluído mas nenhum arquivo encontrado.")

    except Exception as e:
        raise HTTPException(400, f"Erro ao baixar do Instagram: {str(e)}")


class InstagramURL(BaseModel):
    url: str


@app.post("/instagram-extract")
def instagram_extract(body: InstagramURL):
    """Baixa mídia do Instagram (Reels, Stories, Posts/Carrosséis)."""
    cleaned = clean_instagram_url(body.url)

    if not is_instagram_supported(cleaned):
        raise HTTPException(400, "URL não suportada. Aceitamos Reels (/reel/), Stories (/stories/) e Posts/Carrosséis (/p/) públicos.")

    is_story = "/stories/" in cleaned.lower() or "/story/" in cleaned.lower()
    shortcode = extract_shortcode(cleaned)

    job_id = str(uuid.uuid4())[:8]
    out_dir = UPLOAD_DIR / job_id
    out_dir.mkdir(parents=True)

    # Estratégia 1: instaloader (se credenciais configuradas)
    if INSTAGRAM_USER and INSTAGRAM_PASS:
        try:
            result = download_with_instaloader(shortcode, out_dir, is_story)
            media_path = result["path"]
            media_type = result["media_type"]

            jobs[job_id] = {
                "status": "uploaded", "step": 0,
                "message": f"Mídia do Instagram baixada ({media_type})",
                "progress": 0, "video_path": media_path, "out_dir": str(out_dir),
                "source": "instagram", "media_type": media_type
            }

            return {
                "job_id": job_id,
                "filename": Path(media_path).name,
                "media_type": media_type
            }
        except HTTPException:
            raise
        except Exception as e:
            # Fallback para yt-dlp
            pass

    # Estratégia 2: yt-dlp (fallback)
    output_template = str(out_dir / "%(title)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "-f", "best[height<=1080]/best",
        "-o", output_template,
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--socket-timeout", "30",
    ]

    if INSTAGRAM_COOKIES:
        cookie_file = out_dir / "cookies.txt"
        cookie_file.write_text(INSTAGRAM_COOKIES, encoding="utf-8")
        cmd.extend(["--cookies", str(cookie_file)])

    cmd.append(cleaned)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        if result.returncode != 0:
            stderr = result.stderr[-800:]
            if "empty media response" in stderr.lower() or "not granting access" in stderr.lower():
                hint = "\n\nDica: Configure INSTAGRAM_USER e INSTAGRAM_PASS no Railway para acessar conteúdo que exige login."
            elif "not available" in stderr.lower():
                hint = "\n\nDica: Verifique se a URL está correta e se o conteúdo ainda está disponível."
            else:
                hint = ""
            raise HTTPException(400, f"Não foi possível baixar o conteúdo.\n\n{stderr}{hint}")

        media_files = (
            list(out_dir.glob("*.mp4")) + list(out_dir.glob("*.mkv")) +
            list(out_dir.glob("*.webm")) + list(out_dir.glob("*.jpg")) +
            list(out_dir.glob("*.jpeg")) + list(out_dir.glob("*.png")) +
            list(out_dir.glob("*.mp3")) + list(out_dir.glob("*.wav"))
        )
        media_files = [f for f in media_files if f.name != "cookies.txt"]

        if not media_files:
            raise HTTPException(400, "Download concluído mas nenhum arquivo de mídia encontrado.")

        media_path = str(media_files[0])
        ext = media_files[0].suffix.lower()
        media_type = "video" if ext in (".mp4", ".mkv", ".webm") else ("audio" if ext in (".mp3", ".wav") else "image")

        jobs[job_id] = {
            "status": "uploaded", "step": 0,
            "message": f"Mídia do Instagram baixada ({media_type})",
            "progress": 0, "video_path": media_path, "out_dir": str(out_dir),
            "source": "instagram", "media_type": media_type
        }

        return {
            "job_id": job_id,
            "filename": media_files[0].name,
            "media_type": media_type
        }

    except subprocess.TimeoutExpired:
        raise HTTPException(408, "Timeout ao baixar o conteúdo do Instagram.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erro ao processar URL: {str(e)}")


# ─── IA Caption generation ───────────────────────────────────────────────────

CAPTION_PROMPT = """Você é um copywriter especializado em legendas para Instagram.

Com base na transcrição abaixo de um vídeo, crie uma legenda humanizada, vendedora e natural para o Instagram, seguindo EXATAMENTE esta estrutura:

**Regras obrigatórias:**
1. Comece com uma frase de impacto chamando atenção para o produto, serviço ou oferta.
2. Apresente o produto/serviço principal de forma simples e desejável.
3. Destaque os principais benefícios (conforto, praticidade, qualidade, economia, beleza, exclusividade ou resultado).
4. Se houver um segundo produto/serviço no vídeo, apresente como complemento ideal.
5. Use linguagem leve, comercial e próxima do público.
6. Finalize com uma chamada para ação (chamar no WhatsApp, visitar a loja, comentar ou garantir o produto).
7. Use emojis com moderação, combinando com o nicho.
8. NÃO invente informações que não estejam na transcrição.

**Limites de tamanho (OBRIGATÓRIO):**
- Máximo 4 parágrafos no total.
- Máximo 2 frases por parágrafo.
- Máximo 600 caracteres no total.
- Cada parágrafo: no máximo 1-2 linhas (30-50 palavras por parágrafo).
- Se houver muitos produtos no vídeo, cite no máximo 3. Seja conciso.
- Corte adjetivos repetidos. Vá direto ao ponto.

**Formato de saída:**
- Primeiro parágrafo: chamada principal (1-2 frases curtas, máx 30 palavras)
- Segundo parágrafo: apresentação do produto/serviço (máx 50 palavras)
- Terceiro parágrafo: benefícios e diferenciais (máx 50 palavras)
- Quarto parágrafo: chamada para ação (1 frase, máx 25 palavras)

Retorne APENAS a legenda pronta, sem títulos, sem markdown, sem aspas ao redor. Texto pronto para copiar e colar.

Transcrição do vídeo:
{transcription}"""


class CaptionRequest(BaseModel):
    segments: list


@app.post("/caption-ia/{job_id}")
def generate_caption(job_id: str, body: CaptionRequest):
    """Gera legenda humanizada com base nos segmentos transcritos."""
    if job_id not in jobs:
        raise HTTPException(404, "Job não encontrado")

    # Concatena transcrição completa
    full_text = " ".join(s.get("text", "") for s in body.segments if s.get("text", "").strip())
    if not full_text.strip():
        raise HTTPException(400, "Nenhum texto na transcrição para gerar legenda.")

    try:
        client = genai.Client(api_key=os.getenv("GEMINI_KEY"))
        prompt = CAPTION_PROMPT.format(transcription=full_text)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        caption = response.text.strip()
        # Remove possíveis ```markdown wrappers
        caption = re.sub(r"^```(?:markdown)?\s*", "", caption)
        caption = re.sub(r"\s*```$", "", caption)

        return {
            "caption": caption.strip(),
            "transcription": full_text
        }

    except Exception as e:
        print(f"ERRO CAPTION IA: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        raise HTTPException(500, f"Erro ao gerar legenda: {str(e)}")


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False, log_level="info", access_log=True)
